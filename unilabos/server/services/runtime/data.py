"""``runtime.db`` 的微后端控制服务。

同库的 workflow / registry 域共享本服务的 connection 与 write_lock
（单连接单写者）：构造它们时把本服务实例作为 ``database`` 传入。
``find_*`` 返回 Optional，``get_*`` 在缺失时抛 ``RuntimeNotFoundError``。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from unilabos.server.database.sqlite_domain import DomainDatabase, SqliteDomain
from unilabos.server.database.tables.runtime import (
    RUNTIME_DATABASE,
    AdapterCommandOutboxRecord,
    BackendEventOutboxRecord,
    BackendSessionRecord,
    CommandInboxRecord,
    ExecutionJobRecord,
    ExecutorEndpointRecord,
)
from unilabos.protocol.base import canonical_hash, canonical_json
from unilabos.protocol.runtime import (
    AdapterCommandAck,
    AdapterCommandClaim,
    AdapterCommandEnqueue,
    BackendEventAck,
    BackendEventClaim,
    BackendEventEnqueue,
    BackendSessionUpsert,
    CommandEnvelope,
    CommandReceipt,
    EndpointSnapshotResult,
    EndpointSnapshotUpsert,
    ErrorGateDecision,
    ErrorGateOpen,
    ErrorGateResume,
    ExecutionJobCancel,
    ExecutionJobCreate,
    ExecutionJobFeedback,
    ExecutionJobTransition,
)


def _load_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    return json.loads(str(value))


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


class RuntimeServiceError(RuntimeError):
    code = "runtime_error"


class RuntimeNotFoundError(RuntimeServiceError):
    code = "not_found"


class RuntimeConflictError(RuntimeServiceError):
    code = "conflict"


class RuntimeValidationError(RuntimeServiceError):
    code = "invalid_runtime_request"


class RuntimeService(SqliteDomain):
    """Session、endpoint、命令、job 和可靠 outbox 的唯一写入口。"""

    def __init__(self, database: DomainDatabase):
        super().__init__(database, RUNTIME_DATABASE)

    @staticmethod
    def _now_ms(observed_at_ms: int = 0) -> int:
        return max(int(time.time() * 1000), observed_at_ms)

    @staticmethod
    def _require_version(actual: int, expected: int, aggregate: str) -> None:
        if actual != expected:
            raise RuntimeConflictError(
                f"{aggregate} version is {actual}, expected {expected}"
            )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 1000:
            raise RuntimeValidationError("limit must be between 1 and 1000")

    # -- Backend session：行访问 ------------------------------------------

    @staticmethod
    def _session(row: sqlite3.Row) -> BackendSessionRecord:
        return BackendSessionRecord.model_validate(dict(row))

    def find_session(self, session_uuid: str) -> Optional[BackendSessionRecord]:
        row = self.connection.execute(
            "SELECT * FROM backend_session WHERE session_uuid=?", (session_uuid,)
        ).fetchone()
        return self._session(row) if row is not None else None

    def _insert_session(self, record: BackendSessionRecord) -> None:
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

    def _update_session(
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

    def _disconnect_other_active_sessions(
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

    # -- Backend session：业务 --------------------------------------------

    def upsert_backend_session(
        self, value: BackendSessionUpsert
    ) -> BackendSessionRecord:
        timestamp = self._now_ms(value.observed_at_ms)
        with self.write():
            current = self.find_session(value.session_uuid)
            if current is not None:
                # session 跨 WS 重连稳定：同一 session 每次连接换 connection_epoch 是常态
                # （Edge 重启 / 网络抖动后重连），只有归属（edge / backend / 权威代际）
                # 变了才算被另一个 Backend 绑走。
                current_identity = (
                    current.edge_uuid,
                    current.backend_uri,
                    current.authority_epoch,
                )
                requested_identity = (
                    value.edge_uuid,
                    value.backend_uri,
                    value.authority_epoch,
                )
                if current_identity != requested_identity:
                    raise RuntimeConflictError(
                        "session_uuid was already bound to another backend connection"
                    )

            if value.state == "active":
                self._disconnect_other_active_sessions(
                    value.edge_uuid,
                    value.session_uuid,
                    disconnected_at_ms=timestamp,
                )

            connected_at_ms = value.connected_at_ms
            if current is not None and connected_at_ms is None:
                connected_at_ms = current.connected_at_ms
            if connected_at_ms is None and value.state in {"active", "reconciling"}:
                connected_at_ms = timestamp

            command_cursor = max(
                value.command_cursor,
                current.command_cursor if current is not None else 0,
            )
            event_send_cursor = max(
                value.event_send_cursor,
                current.event_send_cursor if current is not None else 0,
            )
            event_ack_sequence = max(
                value.event_ack_sequence,
                current.event_ack_sequence if current is not None else 0,
            )
            record = BackendSessionRecord(
                session_uuid=value.session_uuid,
                edge_uuid=value.edge_uuid,
                backend_uri=value.backend_uri,
                authority_epoch=value.authority_epoch,
                connection_epoch=value.connection_epoch,
                state=value.state,
                command_cursor=command_cursor,
                event_send_cursor=event_send_cursor,
                event_ack_sequence=event_ack_sequence,
                connected_at_ms=connected_at_ms,
                disconnected_at_ms=value.disconnected_at_ms,
                last_seen_at_ms=timestamp,
                version=1 if current is None else current.version + 1,
            )
            if current is None:
                self._insert_session(record)
            else:
                self._update_session(record, expected_version=current.version)
            return record

    def get_backend_session(self, session_uuid: str) -> BackendSessionRecord:
        record = self.find_session(session_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"backend session {session_uuid!r} not found")
        return record

    def list_backend_sessions(
        self,
        *,
        edge_uuid: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> list[BackendSessionRecord]:
        self._validate_limit(limit)
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

    # -- Endpoint snapshot：行访问 -----------------------------------------

    @staticmethod
    def _endpoint(row: sqlite3.Row) -> ExecutorEndpointRecord:
        values = dict(row)
        values["device_routes"] = _load_json(values.pop("device_routes_json"), [])
        values["action_capabilities"] = _load_json(
            values.pop("action_capabilities_json"), []
        )
        values["config"] = _load_json(values.pop("config_json"), {})
        return ExecutorEndpointRecord.model_validate(values)

    def find_endpoint(self, endpoint_uuid: str) -> Optional[ExecutorEndpointRecord]:
        row = self.connection.execute(
            "SELECT * FROM executor_endpoint WHERE endpoint_uuid=?", (endpoint_uuid,)
        ).fetchone()
        return self._endpoint(row) if row is not None else None

    def find_endpoint_by_identity(
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

    @staticmethod
    def _endpoint_values(record: ExecutorEndpointRecord) -> dict[str, Any]:
        values = record.model_dump(mode="json")
        values["device_routes_json"] = canonical_json(values.pop("device_routes"))
        values["action_capabilities_json"] = canonical_json(
            values.pop("action_capabilities")
        )
        values["config_json"] = canonical_json(values.pop("config"))
        return values

    def _insert_endpoint(self, record: ExecutorEndpointRecord) -> None:
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

    def _update_endpoint(
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

    # -- Endpoint snapshot：业务 --------------------------------------------

    @staticmethod
    def _endpoint_snapshot_hash(value: EndpointSnapshotUpsert) -> str:
        data = value.model_dump(
            mode="json",
            exclude={"observed_at_ms", "reconciled_at_ms"},
            exclude_none=False,
        )
        return canonical_hash(data)

    def upsert_endpoint_snapshot(
        self, value: EndpointSnapshotUpsert
    ) -> EndpointSnapshotResult:
        timestamp = self._now_ms(value.observed_at_ms)
        snapshot_hash = self._endpoint_snapshot_hash(value)
        with self.write():
            current = self.find_endpoint(value.endpoint_uuid)
            by_identity = self.find_endpoint_by_identity(
                value.transport, value.host_uuid, value.instance_name
            )
            if (
                by_identity is not None
                and by_identity.endpoint_uuid != value.endpoint_uuid
            ):
                raise RuntimeConflictError(
                    "transport/host/instance identity belongs to another endpoint_uuid"
                )
            if current is not None:
                identity = (
                    current.transport,
                    current.host_uuid,
                    current.instance_name,
                )
                requested = (value.transport, value.host_uuid, value.instance_name)
                if identity != requested:
                    raise RuntimeConflictError(
                        "endpoint_uuid was already bound to another executor identity"
                    )

            changed = current is None or current.snapshot_hash != snapshot_hash
            same_adapter_epoch = (
                current is not None and current.adapter_epoch == value.adapter_epoch
            )
            record = ExecutorEndpointRecord(
                endpoint_uuid=value.endpoint_uuid,
                transport=value.transport,
                host_uuid=value.host_uuid,
                instance_name=value.instance_name,
                authority_epoch=value.authority_epoch,
                adapter_epoch=value.adapter_epoch,
                adapter_event_cursor=(
                    current.adapter_event_cursor if same_adapter_epoch else 0
                ),
                reconciliation_generation=value.reconciliation_generation,
                state=value.state,
                device_routes=value.device_routes,
                action_capabilities=value.action_capabilities,
                config=value.config,
                snapshot_hash=snapshot_hash,
                registered_at_ms=(
                    current.registered_at_ms if current is not None else timestamp
                ),
                last_seen_at_ms=timestamp,
                reconciled_at_ms=value.reconciled_at_ms,
                version=(
                    1
                    if current is None
                    else current.version + 1
                    if changed
                    else current.version
                ),
            )
            if current is None:
                self._insert_endpoint(record)
            else:
                self._update_endpoint(record, expected_version=current.version)
            return EndpointSnapshotResult(endpoint=record, changed=changed)

    def get_endpoint_snapshot(self, endpoint_uuid: str) -> ExecutorEndpointRecord:
        record = self.find_endpoint(endpoint_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"endpoint {endpoint_uuid!r} not found")
        return record

    def list_endpoint_snapshots(
        self,
        *,
        transport: Optional[str] = None,
        state: Optional[str] = None,
        host_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutorEndpointRecord]:
        self._validate_limit(limit)
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

    # -- Command inbox：行访问 ----------------------------------------------

    @staticmethod
    def _command(row: sqlite3.Row) -> CommandInboxRecord:
        values = dict(row)
        values["summary"] = _load_json(values.pop("summary_json"), {})
        return CommandInboxRecord.model_validate(values)

    def find_command(self, command_uuid: str) -> Optional[CommandInboxRecord]:
        row = self.connection.execute(
            "SELECT * FROM command_inbox WHERE command_uuid=?", (command_uuid,)
        ).fetchone()
        return self._command(row) if row is not None else None

    def find_command_by_sequence(
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

    def _insert_command(self, record: CommandInboxRecord) -> None:
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

    def _update_command(
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

    # -- Command inbox：业务 -------------------------------------------------

    @staticmethod
    def _command_fingerprint(value: CommandEnvelope) -> str:
        return canonical_hash(value.model_dump(mode="json", exclude={"received_at_ms"}))

    def receive_command(self, value: CommandEnvelope) -> CommandReceipt:
        fingerprint = self._command_fingerprint(value)
        timestamp = self._now_ms(value.received_at_ms)
        with self.write():
            current = self.find_command(value.command_uuid)
            if current is not None:
                if (
                    current.command_fingerprint != fingerprint
                    or current.session_uuid != value.session_uuid
                    or current.backend_sequence != value.backend_sequence
                ):
                    raise RuntimeConflictError(
                        "command_uuid was replayed with different content"
                    )
                return CommandReceipt(
                    command_uuid=current.command_uuid,
                    backend_sequence=current.backend_sequence,
                    command_fingerprint=current.command_fingerprint,
                    replayed=True,
                )

            occupied = self.find_command_by_sequence(
                value.session_uuid, value.backend_sequence
            )
            if occupied is not None:
                raise RuntimeConflictError(
                    "backend sequence was already used by another command"
                )
            session = self.find_session(value.session_uuid)
            if session is None:
                raise RuntimeNotFoundError(
                    f"backend session {value.session_uuid!r} not found"
                )
            expected_sequence = session.command_cursor + 1
            if value.backend_sequence != expected_sequence:
                raise RuntimeConflictError(
                    f"backend sequence is {value.backend_sequence}, "
                    f"expected {expected_sequence}"
                )

            record = CommandInboxRecord(
                command_uuid=value.command_uuid,
                session_uuid=value.session_uuid,
                backend_sequence=value.backend_sequence,
                command_type=value.command_type,
                job_uuid=value.job_uuid,
                payload_uuid=value.payload_uuid,
                payload_sha256=value.payload_sha256,
                command_fingerprint=fingerprint,
                summary=value.summary,
                traceparent=value.traceparent,
                status="received",
                received_at_ms=timestamp,
            )
            self._insert_command(record)
            self._update_session(
                session.model_copy(
                    update={
                        "command_cursor": value.backend_sequence,
                        "last_seen_at_ms": timestamp,
                        "version": session.version + 1,
                    }
                ),
                expected_version=session.version,
            )
            return CommandReceipt(
                command_uuid=value.command_uuid,
                backend_sequence=value.backend_sequence,
                command_fingerprint=fingerprint,
            )

    def get_command(self, command_uuid: str) -> CommandInboxRecord:
        record = self.find_command(command_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"command {command_uuid!r} not found")
        return record

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
        if after_sequence < 0:
            raise RuntimeValidationError("after_sequence cannot be negative")
        if after_sequence and session_uuid is None:
            raise RuntimeValidationError(
                "after_sequence requires session_uuid because backend sequence is "
                "session-local"
            )
        self._validate_limit(limit)
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

    def _complete_command(
        self,
        command: CommandInboxRecord,
        *,
        timestamp: int,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> CommandInboxRecord:
        status = "rejected" if error_code is not None else "applied"
        if command.status in {"applied", "rejected"}:
            if command.status != status:
                raise RuntimeConflictError(
                    "command already has another terminal status"
                )
            return command
        updated = command.model_copy(
            update={
                "status": status,
                "applied_at_ms": timestamp,
                "error_code": error_code,
                "error_message": error_message,
                "version": command.version + 1,
            }
        )
        self._update_command(updated, expected_version=command.version)
        return updated

    # -- Execution job：行访问 ------------------------------------------------

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

    def find_job(self, job_uuid: str) -> Optional[ExecutionJobRecord]:
        row = self.connection.execute(
            "SELECT * FROM execution_job WHERE job_uuid=?", (job_uuid,)
        ).fetchone()
        return self._job(row) if row is not None else None

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

    def _insert_job(self, record: ExecutionJobRecord) -> None:
        values = self._job_values(record)
        self.connection.execute(
            """
            INSERT INTO execution_job(
                job_uuid,task_uuid,node_uuid,attempt_group_uuid,retry_of_job_uuid,
                attempt_no,attempt_trigger,execute_command_uuid,device_uuid,action_name,
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
                :attempt_no,:attempt_trigger,:execute_command_uuid,:device_uuid,:action_name,
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

    def _update_job(
        self, record: ExecutionJobRecord, *, expected_version: int
    ) -> None:
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

    # -- Execution job：业务 ---------------------------------------------------

    def create_execution_job(self, value: ExecutionJobCreate) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.accepted_at_ms)
        with self.write():
            command = self.find_command(value.execute_command_uuid)
            if command is None:
                raise RuntimeNotFoundError(
                    f"execute command {value.execute_command_uuid!r} not found"
                )
            if (
                command.command_type != "execute_job"
                or command.job_uuid != value.job_uuid
            ):
                raise RuntimeValidationError(
                    "execute command type/job_uuid does not match execution job"
                )

            current = self.find_job(value.job_uuid)
            if current is not None:
                if current.execute_command_uuid != value.execute_command_uuid:
                    raise RuntimeConflictError(
                        "job_uuid was already created by another command"
                    )
                return current
            if command.status in {"applied", "rejected"}:
                raise RuntimeConflictError(
                    "terminal execute command has no matching execution job"
                )

            if value.retry_of_job_uuid is not None:
                previous = self.find_job(value.retry_of_job_uuid)
                if previous is None:
                    raise RuntimeNotFoundError(
                        f"retry source {value.retry_of_job_uuid!r} not found"
                    )
                if previous.status not in {"failed", "canceled", "rejected"}:
                    raise RuntimeConflictError("retry source is not terminal")
                if (
                    previous.attempt_group_uuid != value.attempt_group_uuid
                    or previous.task_uuid != value.task_uuid
                    or previous.node_uuid != value.node_uuid
                    or value.attempt_no != previous.attempt_no + 1
                ):
                    raise RuntimeValidationError(
                        "retry must continue the same task/node attempt group"
                    )

            if value.endpoint_uuid is not None:
                endpoint = self.find_endpoint(value.endpoint_uuid)
                if endpoint is None:
                    raise RuntimeNotFoundError(
                        f"endpoint {value.endpoint_uuid!r} not found"
                    )
                if endpoint.transport != value.transport:
                    raise RuntimeValidationError(
                        "job transport does not match executor endpoint"
                    )

            record = ExecutionJobRecord(
                **value.model_dump(mode="json", exclude={"accepted_at_ms"}),
                status="accepted",
                accepted_at_ms=timestamp,
            )
            try:
                self._insert_job(record)
            except sqlite3.IntegrityError as exc:
                raise RuntimeConflictError(str(exc)) from exc
            self._complete_command(command, timestamp=timestamp)
            return record

    def get_execution_job(self, job_uuid: str) -> ExecutionJobRecord:
        record = self.find_job(job_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
        return record

    def list_execution_jobs(
        self,
        *,
        status: Optional[str] = None,
        device_uuid: Optional[str] = None,
        endpoint_uuid: Optional[str] = None,
        retry_of_job_uuid: Optional[str] = None,
        attempt_group_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionJobRecord]:
        self._validate_limit(limit)
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

    _TRANSITIONS = {
        "accepted": {"dispatch_pending", "rejected", "canceled"},
        "dispatch_pending": {
            "execution_unknown",
            "dispatched",
            "succeeded",
            "rejected",
            "canceled",
        },
        "dispatched": {
            "running",
            "succeeded",
            "execution_unknown",
            "canceled",
        },
        "running": {"succeeded", "execution_unknown", "canceled"},
        "failure_waiting": {"execution_unknown", "canceled"},
        "terminal_waiting": {"succeeded", "failed", "canceled"},
        "execution_unknown": {"running", "succeeded", "canceled"},
    }

    def transition_execution_job(
        self, job_uuid: str, value: ExecutionJobTransition
    ) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.occurred_at_ms)
        with self.write():
            current = self.find_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            self._require_version(current.version, value.expected_version, "job")
            if value.status not in self._TRANSITIONS.get(current.status, set()):
                raise RuntimeConflictError(
                    f"cannot transition job from {current.status} to {value.status}"
                )
            if (
                value.status == "failed"
                and current.terminal_gate_state != "released_failed"
            ):
                raise RuntimeConflictError(
                    "failed cannot be persisted before backend releases the error gate"
                )
            if value.status == "succeeded" and current.terminal_gate_state not in {
                "none",
                "result_replaced",
            }:
                raise RuntimeConflictError("open error gate does not allow succeeded")
            if value.status == "canceled" and current.terminal_gate_state not in {
                "none",
                "canceled",
            }:
                raise RuntimeConflictError("open error gate does not allow canceled")

            updates: dict[str, Any] = {
                "status": value.status,
                "version": current.version + 1,
            }
            for field in (
                "scheduler_status_version",
                "feedback_sequence",
                "result_uuid",
                "error_code",
                "error_summary",
            ):
                supplied = getattr(value, field)
                if supplied is not None:
                    updates[field] = supplied
            if value.status == "dispatched":
                updates["dispatched_at_ms"] = timestamp
            elif value.status == "running":
                updates["started_at_ms"] = timestamp
            elif value.status in {"succeeded", "failed", "canceled", "rejected"}:
                updates["finished_at_ms"] = timestamp
            updated = current.model_copy(update=updates)
            self._update_job(updated, expected_version=current.version)
            return updated

    def record_execution_feedback(
        self, job_uuid: str, value: ExecutionJobFeedback
    ) -> ExecutionJobRecord:
        """单调推进 feedback cursor，并为每次出站变更分配新的 job version。"""

        with self.write():
            current = self.find_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            self._require_version(current.version, value.expected_version, "job")
            if current.status not in {"dispatched", "running", "execution_unknown"}:
                raise RuntimeConflictError(
                    f"job status {current.status!r} cannot accept feedback"
                )
            expected_sequence = current.feedback_sequence + 1
            if value.feedback_sequence != expected_sequence:
                raise RuntimeConflictError(
                    f"feedback sequence is {value.feedback_sequence}, "
                    f"expected {expected_sequence}"
                )
            updated = current.model_copy(
                update={
                    "feedback_sequence": value.feedback_sequence,
                    "version": current.version + 1,
                }
            )
            self._update_job(updated, expected_version=current.version)
            return updated

    def request_execution_cancel(
        self, job_uuid: str, value: ExecutionJobCancel
    ) -> ExecutionJobRecord:
        """持久化 Backend cancel 命令，再由 endpoint adapter outbox 执行。"""

        timestamp = self._now_ms(value.requested_at_ms)
        with self.write():
            current = self.find_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            command = self.find_command(value.cancel_command_uuid)
            if command is None:
                raise RuntimeNotFoundError(
                    f"cancel command {value.cancel_command_uuid!r} not found"
                )
            if command.command_type != "cancel_job" or command.job_uuid != job_uuid:
                raise RuntimeValidationError(
                    "cancel command type/job_uuid does not match execution job"
                )
            if current.status in {"succeeded", "failed", "canceled", "rejected"}:
                self._complete_command(command, timestamp=timestamp)
                return current
            self._require_version(current.version, value.expected_version, "job")
            if current.status == "terminal_waiting":
                raise RuntimeConflictError(
                    "terminal-waiting job must be resolved through its error gate"
                )
            if current.endpoint_uuid is None:
                raise RuntimeValidationError(
                    "routed endpoint is required to cancel execution"
                )
            endpoint = self.find_endpoint(current.endpoint_uuid)
            if endpoint is None:
                raise RuntimeNotFoundError(
                    f"endpoint {current.endpoint_uuid!r} not found"
                )
            self._enqueue_adapter_command_locked(
                AdapterCommandEnqueue(
                    adapter_command_uuid=value.adapter_command_uuid,
                    job_uuid=job_uuid,
                    endpoint_uuid=current.endpoint_uuid,
                    source_command_uuid=value.cancel_command_uuid,
                    target_adapter_epoch=endpoint.adapter_epoch,
                    command_type="cancel",
                    payload_uuid=value.payload_uuid,
                    available_at_ms=timestamp,
                ),
                timestamp=timestamp,
            )
            self._complete_command(command, timestamp=timestamp)
            return current

    # -- Backend-controlled terminal error gate -------------------------

    def open_error_gate(
        self, job_uuid: str, value: ErrorGateOpen
    ) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.opened_at_ms)
        with self.write():
            current = self.find_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            if current.terminal_gate_state != "none":
                if (
                    current.terminal_error_uuid == value.error_uuid
                    and current.terminal_request_event_uuid == value.request_event_uuid
                ):
                    return current
                raise RuntimeConflictError(
                    "job already has another terminal error gate"
                )
            self._require_version(current.version, value.expected_version, "job")
            if current.status not in {
                "accepted",
                "dispatch_pending",
                "dispatched",
                "running",
                "execution_unknown",
            }:
                raise RuntimeConflictError(
                    f"job status {current.status!r} cannot open an error gate"
                )

            updated = current.model_copy(
                update={
                    "status": "terminal_waiting",
                    "error_code": value.error_code,
                    "error_summary": value.error_summary,
                    "terminal_gate_state": "waiting_backend",
                    "terminal_error_uuid": value.error_uuid,
                    "terminal_required_scheduler_revision": (
                        value.required_scheduler_revision
                    ),
                    "terminal_request_event_uuid": value.request_event_uuid,
                    "terminal_opened_at_ms": timestamp,
                    "version": current.version + 1,
                }
            )
            self._update_job(updated, expected_version=current.version)
            summary = {
                **value.summary,
                "error_uuid": value.error_uuid,
                "error_code": value.error_code,
                "error_summary": value.error_summary,
                "required_scheduler_revision": value.required_scheduler_revision,
            }
            self._enqueue_backend_event_locked(
                BackendEventEnqueue(
                    event_uuid=value.request_event_uuid,
                    event_type="execution.error_pending",
                    aggregate_type="execution_job",
                    aggregate_uuid=job_uuid,
                    aggregate_version=updated.version,
                    job_uuid=job_uuid,
                    summary=summary,
                    detail_payload_uuid=value.detail_payload_uuid,
                    available_at_ms=timestamp,
                ),
                timestamp=timestamp,
            )
            return updated

    def decide_error_gate(
        self, job_uuid: str, value: ErrorGateDecision
    ) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.resolved_at_ms)
        with self.write():
            current = self.find_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            self._require_version(current.version, value.expected_version, "job")
            if current.terminal_gate_state not in {
                "waiting_backend",
                "backend_confirmed",
            }:
                raise RuntimeConflictError("job has no backend-waiting error gate")
            required = current.terminal_required_scheduler_revision or 0
            if value.confirmed_scheduler_revision < required:
                raise RuntimeConflictError(
                    "scheduler revision has not reached the terminal gate requirement"
                )
            command = self.find_command(value.decision_command_uuid)
            if command is None:
                raise RuntimeNotFoundError(
                    f"decision command {value.decision_command_uuid!r} not found"
                )
            expected_type = {
                "release_failed": "release_failed",
                "replace_result": "replace_result",
                "cancel": "cancel_job",
            }[value.action]
            if command.command_type != expected_type or command.job_uuid != job_uuid:
                raise RuntimeValidationError(
                    "decision command type/job_uuid does not match terminal action"
                )
            if current.endpoint_uuid is None:
                raise RuntimeValidationError(
                    "routed endpoint is required to release an execution error gate"
                )

            gate_state = {
                "release_failed": "released_failed",
                "replace_result": "result_replaced",
                "cancel": "canceled",
            }[value.action]
            decision = {**value.decision, "action": value.action}
            updated = current.model_copy(
                update={
                    "terminal_gate_state": gate_state,
                    "terminal_confirmed_scheduler_revision": (
                        value.confirmed_scheduler_revision
                    ),
                    "terminal_decision_command_uuid": value.decision_command_uuid,
                    "terminal_decision": decision,
                    "terminal_resolved_at_ms": timestamp,
                    "result_uuid": value.result_uuid or current.result_uuid,
                    "version": current.version + 1,
                }
            )
            self._update_job(updated, expected_version=current.version)
            endpoint = self.find_endpoint(current.endpoint_uuid)
            if endpoint is None:
                raise RuntimeNotFoundError(
                    f"endpoint {current.endpoint_uuid!r} not found"
                )
            self._enqueue_adapter_command_locked(
                AdapterCommandEnqueue(
                    adapter_command_uuid=value.adapter_command_uuid,
                    job_uuid=job_uuid,
                    endpoint_uuid=current.endpoint_uuid,
                    source_command_uuid=value.decision_command_uuid,
                    target_adapter_epoch=endpoint.adapter_epoch,
                    command_type=(
                        "cancel" if value.action == "cancel" else value.action
                    ),
                    payload_uuid=value.payload_uuid,
                    available_at_ms=timestamp,
                ),
                timestamp=timestamp,
            )
            self._complete_command(command, timestamp=timestamp)
            return updated

    def resume_error_gate(
        self, job_uuid: str, value: ErrorGateResume
    ) -> ExecutionJobRecord:
        """关闭 ``execution_timeout`` 软超时打开的终态闸门：动作从未停止，job 回到 running。

        与 :meth:`decide_error_gate` 不同，这里不产生终态：闸门字段全部清空，后续真实
        结果或再次超时都可以重新打开闸门。带 ``decision_command_uuid`` 时是 Backend 的
        ``resume_pending`` 命令，需向执行面投递同名 adapter 命令让它重新计时；不带时是
        执行面因动作真实完成而自行收回决策，只需同步本地状态。两种情况都向 Backend 发
        ``execution.error_resumed`` 事件，让权威撤销待决策并把节点运行收回 running。
        """

        timestamp = self._now_ms(value.resolved_at_ms)
        with self.write():
            current = self.find_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            self._require_version(current.version, value.expected_version, "job")
            if current.terminal_gate_state not in {"waiting_backend", "backend_confirmed"}:
                raise RuntimeConflictError("job has no backend-waiting error gate")
            if current.terminal_error_uuid != value.error_uuid:
                raise RuntimeConflictError("resume does not match the pending job error")
            if current.status != "terminal_waiting":
                raise RuntimeConflictError(
                    f"job status {current.status!r} cannot resume from an error gate"
                )
            command = None
            if value.decision_command_uuid is not None:
                command = self.find_command(value.decision_command_uuid)
                if command is None:
                    raise RuntimeNotFoundError(
                        f"decision command {value.decision_command_uuid!r} not found"
                    )
                if command.command_type != "resume_pending" or command.job_uuid != job_uuid:
                    raise RuntimeValidationError(
                        "resume command type/job_uuid does not match the job"
                    )
                if current.endpoint_uuid is None:
                    raise RuntimeValidationError(
                        "routed endpoint is required to resume an execution error gate"
                    )

            updated = current.model_copy(
                update={
                    "status": "running",
                    "error_code": None,
                    "error_summary": None,
                    "terminal_gate_state": "none",
                    "terminal_error_uuid": None,
                    "terminal_required_scheduler_revision": None,
                    "terminal_request_event_uuid": None,
                    "terminal_opened_at_ms": None,
                    "terminal_confirmed_scheduler_revision": None,
                    "terminal_decision_command_uuid": None,
                    "terminal_decision": {},
                    "terminal_resolved_at_ms": None,
                    "version": current.version + 1,
                }
            )
            self._update_job(updated, expected_version=current.version)
            if command is not None and value.adapter_command_uuid is not None:
                endpoint = self.find_endpoint(current.endpoint_uuid or "")
                if endpoint is None:
                    raise RuntimeNotFoundError(
                        f"endpoint {current.endpoint_uuid!r} not found"
                    )
                self._enqueue_adapter_command_locked(
                    AdapterCommandEnqueue(
                        adapter_command_uuid=value.adapter_command_uuid,
                        job_uuid=job_uuid,
                        endpoint_uuid=current.endpoint_uuid or "",
                        source_command_uuid=value.decision_command_uuid,
                        target_adapter_epoch=endpoint.adapter_epoch,
                        command_type="resume_pending",
                        payload_uuid=value.payload_uuid,
                        available_at_ms=timestamp,
                    ),
                    timestamp=timestamp,
                )
                self._complete_command(command, timestamp=timestamp)
            self._enqueue_backend_event_locked(
                BackendEventEnqueue(
                    event_uuid=str(uuid.uuid4()),
                    event_type="execution.error_resumed",
                    aggregate_type="execution_job",
                    aggregate_uuid=job_uuid,
                    aggregate_version=updated.version,
                    job_uuid=job_uuid,
                    summary={
                        "status": "running",
                        "error_uuid": value.error_uuid,
                        "reason": value.reason,
                        **value.decision,
                    },
                    available_at_ms=timestamp,
                ),
                timestamp=timestamp,
            )
            return updated

    # -- Adapter command outbox -----------------------------------------

    @staticmethod
    def _adapter_command(row: sqlite3.Row) -> AdapterCommandOutboxRecord:
        return AdapterCommandOutboxRecord.model_validate(dict(row))

    def find_adapter_command(
        self, adapter_command_uuid: str
    ) -> Optional[AdapterCommandOutboxRecord]:
        row = self.connection.execute(
            "SELECT * FROM adapter_command_outbox WHERE adapter_command_uuid=?",
            (adapter_command_uuid,),
        ).fetchone()
        return self._adapter_command(row) if row is not None else None

    def _insert_adapter_command_row(
        self, record: AdapterCommandOutboxRecord
    ) -> int:
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

    @staticmethod
    def _same_adapter_command(
        current: AdapterCommandOutboxRecord, value: AdapterCommandEnqueue
    ) -> bool:
        fields = (
            "job_uuid",
            "endpoint_uuid",
            "source_command_uuid",
            "trigger_event_uuid",
            "target_adapter_epoch",
            "command_type",
            "payload_uuid",
        )
        return all(getattr(current, field) == getattr(value, field) for field in fields)

    def _enqueue_adapter_command_locked(
        self, value: AdapterCommandEnqueue, *, timestamp: int
    ) -> AdapterCommandOutboxRecord:
        current = self.find_adapter_command(value.adapter_command_uuid)
        if current is not None:
            if not self._same_adapter_command(current, value):
                raise RuntimeConflictError(
                    "adapter_command_uuid was replayed with different content"
                )
            return current
        record = AdapterCommandOutboxRecord(
            **value.model_dump(mode="json"),
            status="pending",
            created_at_ms=timestamp,
        )
        try:
            sequence = self._insert_adapter_command_row(record)
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(str(exc)) from exc
        return record.model_copy(update={"sequence": sequence})

    def enqueue_adapter_command(
        self, value: AdapterCommandEnqueue
    ) -> AdapterCommandOutboxRecord:
        timestamp = self._now_ms(value.available_at_ms)
        with self.write():
            return self._enqueue_adapter_command_locked(value, timestamp=timestamp)

    def get_adapter_command(
        self, adapter_command_uuid: str
    ) -> AdapterCommandOutboxRecord:
        record = self.find_adapter_command(adapter_command_uuid)
        if record is None:
            raise RuntimeNotFoundError(
                f"adapter command {adapter_command_uuid!r} not found"
            )
        return record

    def list_adapter_commands(
        self,
        *,
        endpoint_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[AdapterCommandOutboxRecord]:
        if after_sequence < 0:
            raise RuntimeValidationError("after_sequence cannot be negative")
        self._validate_limit(limit)
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

    def claim_adapter_commands(
        self, value: AdapterCommandClaim
    ) -> list[AdapterCommandOutboxRecord]:
        timestamp = self._now_ms(value.now_ms)
        with self.write():
            if self.find_endpoint(value.endpoint_uuid) is None:
                raise RuntimeNotFoundError(
                    f"endpoint {value.endpoint_uuid!r} not found"
                )
            rows = self.connection.execute(
                """
                SELECT sequence FROM adapter_command_outbox
                WHERE endpoint_uuid=? AND status IN ('pending','sent')
                    AND available_at_ms<=?
                ORDER BY sequence LIMIT ?
                """,
                (value.endpoint_uuid, timestamp, value.limit),
            ).fetchall()
            sequences = [int(row[0]) for row in rows]
            if not sequences:
                return []
            params: list[Any] = [timestamp, timestamp + value.lease_ms, *sequences]
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
        self, value: AdapterCommandAck
    ) -> AdapterCommandOutboxRecord:
        timestamp = self._now_ms(value.acknowledged_at_ms)
        with self.write():
            current = self.find_adapter_command(value.adapter_command_uuid)
            if current is None:
                raise RuntimeNotFoundError(
                    f"adapter command {value.adapter_command_uuid!r} not found"
                )
            if current.status == "acknowledged":
                if current.ack_event_uuid != value.ack_event_uuid:
                    raise RuntimeConflictError(
                        "adapter command was ACKed by another event"
                    )
                return current
            if current.status != "sent":
                raise RuntimeConflictError("adapter command must be claimed before ACK")
            cursor = self.connection.execute(
                """
                UPDATE adapter_command_outbox
                SET status='acknowledged',acked_at_ms=?,ack_event_uuid=?
                WHERE adapter_command_uuid=? AND status='sent'
                """,
                (timestamp, value.ack_event_uuid, value.adapter_command_uuid),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("adapter command is not claimable for ACK")
            acknowledged = self.find_adapter_command(value.adapter_command_uuid)
            assert acknowledged is not None
            return acknowledged

    # -- Backend event outbox -------------------------------------------

    @staticmethod
    def _backend_event(row: sqlite3.Row) -> BackendEventOutboxRecord:
        values = dict(row)
        values["summary"] = _load_json(values.pop("summary_json"), {})
        return BackendEventOutboxRecord.model_validate(values)

    def find_backend_event(
        self, event_uuid: str
    ) -> Optional[BackendEventOutboxRecord]:
        row = self.connection.execute(
            "SELECT * FROM backend_event_outbox WHERE event_uuid=?", (event_uuid,)
        ).fetchone()
        return self._backend_event(row) if row is not None else None

    def _insert_backend_event_row(self, record: BackendEventOutboxRecord) -> int:
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

    @staticmethod
    def _same_backend_event(
        current: BackendEventOutboxRecord, value: BackendEventEnqueue
    ) -> bool:
        fields = (
            "event_type",
            "aggregate_type",
            "aggregate_uuid",
            "aggregate_version",
            "job_uuid",
            "summary",
            "detail_payload_uuid",
            "traceparent",
            "tracestate",
        )
        return all(getattr(current, field) == getattr(value, field) for field in fields)

    def _enqueue_backend_event_locked(
        self, value: BackendEventEnqueue, *, timestamp: int
    ) -> BackendEventOutboxRecord:
        current = self.find_backend_event(value.event_uuid)
        if current is not None:
            if not self._same_backend_event(current, value):
                raise RuntimeConflictError(
                    "event_uuid was replayed with different content"
                )
            return current
        record = BackendEventOutboxRecord(
            **value.model_dump(mode="json"),
            status="pending",
            created_at_ms=timestamp,
        )
        try:
            sequence = self._insert_backend_event_row(record)
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(str(exc)) from exc
        return record.model_copy(update={"sequence": sequence})

    def enqueue_backend_event(
        self, value: BackendEventEnqueue
    ) -> BackendEventOutboxRecord:
        timestamp = self._now_ms(value.available_at_ms)
        with self.write():
            return self._enqueue_backend_event_locked(value, timestamp=timestamp)

    def get_backend_event(self, event_uuid: str) -> BackendEventOutboxRecord:
        record = self.find_backend_event(event_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"backend event {event_uuid!r} not found")
        return record

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
        if after_sequence < 0:
            raise RuntimeValidationError("after_sequence cannot be negative")
        self._validate_limit(limit)
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

    def claim_backend_events(
        self, value: BackendEventClaim
    ) -> list[BackendEventOutboxRecord]:
        timestamp = self._now_ms(value.now_ms)
        with self.write():
            session = self.find_session(value.session_uuid)
            if session is None:
                raise RuntimeNotFoundError(
                    f"backend session {value.session_uuid!r} not found"
                )
            if session.state == "disconnected":
                raise RuntimeConflictError("disconnected session cannot claim events")
            rows = self.connection.execute(
                """
                SELECT sequence FROM backend_event_outbox
                WHERE status IN ('pending','sent') AND available_at_ms<=?
                ORDER BY sequence LIMIT ?
                """,
                (timestamp, value.limit),
            ).fetchall()
            sequences = [int(row[0]) for row in rows]
            events: list[BackendEventOutboxRecord] = []
            if sequences:
                params: list[Any] = [timestamp, timestamp + value.lease_ms, *sequences]
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
                events = [self._backend_event(row) for row in claimed]
            if events:
                last_sequence = max(item.sequence or 0 for item in events)
                self._update_session(
                    session.model_copy(
                        update={
                            "event_send_cursor": max(
                                session.event_send_cursor, last_sequence
                            ),
                            "last_seen_at_ms": timestamp,
                            "version": session.version + 1,
                        }
                    ),
                    expected_version=session.version,
                )
            return events

    def acknowledge_backend_events(self, value: BackendEventAck) -> int:
        timestamp = self._now_ms(value.acknowledged_at_ms)
        with self.write():
            session = self.find_session(value.session_uuid)
            if session is None:
                raise RuntimeNotFoundError(
                    f"backend session {value.session_uuid!r} not found"
                )
            if value.through_sequence > session.event_send_cursor:
                raise RuntimeConflictError("ACK exceeds the event send cursor")
            if value.through_sequence <= session.event_ack_sequence:
                return 0
            cursor = self.connection.execute(
                """
                UPDATE backend_event_outbox
                SET status='acknowledged',acked_at_ms=?
                WHERE sequence<=? AND status='sent'
                """,
                (timestamp, value.through_sequence),
            )
            count = int(cursor.rowcount)
            self._update_session(
                session.model_copy(
                    update={
                        "event_ack_sequence": value.through_sequence,
                        "last_seen_at_ms": timestamp,
                        "version": session.version + 1,
                    }
                ),
                expected_version=session.version,
            )
            return count


__all__ = [
    "RuntimeConflictError",
    "RuntimeNotFoundError",
    "RuntimeService",
    "RuntimeServiceError",
    "RuntimeValidationError",
]
