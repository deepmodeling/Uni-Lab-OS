"""``runtime.db`` 运行控制表：微后端命令与执行控制的表记录与内嵌值对象。

库级规格（``RUNTIME_DATABASE``）由包 ``__init__`` 聚合本模块与同域的
workflow / registry 模块。
"""

from __future__ import annotations

from typing import Annotated, ClassVar, List, Literal, Optional

from pydantic import model_validator
from sqlalchemy import Column, LargeBinary, Text
from sqlmodel import Field

from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    TableObject,
    UnixMilliseconds,
    json_text_column,
)
from unilabos.server.database.schema import (
    SCHEMA_IDENTITY_TABLE,
    TableSpec,
)


Transport = Annotated[Literal["hostlink", "ros2"], Field(sa_type=Text)]


class DeviceRoute(ServerObject):
    """Endpoint 快照内的设备 route，不是独立表记录。"""

    route_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    driver_key: NonEmptyStr
    priority: int = 0
    enabled: bool = True
    selected: bool = False
    config_hash: NonEmptyStr
    config: JsonObject = Field(default_factory=dict)


class DeviceActionCapability(ServerObject):
    """Endpoint 快照内的 action 能力及当前可用性。"""

    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_type: Optional[str] = None
    concurrency_mode: Literal["exclusive", "unbounded"]
    state: Literal["active", "retired"] = "active"
    availability: Literal["free", "busy", "unknown"] = "unknown"
    active_job_uuid: Optional[NonEmptyStr] = None
    descriptor: JsonObject = Field(default_factory=dict)
    descriptor_hash: NonEmptyStr
    observed_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_availability(self) -> "DeviceActionCapability":
        if self.availability == "free" and self.active_job_uuid is not None:
            raise ValueError("free action cannot reference an active job")
        return self


class MaterialBinding(ServerObject):
    """Job 接收时固化的物料绑定快照。"""

    key: NonEmptyStr
    role: NonEmptyStr
    material_uuid: Optional[NonEmptyStr] = None
    site_uuid: Optional[NonEmptyStr] = None
    reservation_uuid: Optional[NonEmptyStr] = None
    quantity: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = None
    snapshot: JsonObject = Field(default_factory=dict)
    snapshot_hash: NonEmptyStr


class BackendSessionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "backend_session"

    session_uuid: NonEmptyStr = Field(primary_key=True)
    edge_uuid: NonEmptyStr
    backend_uri: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr
    state: Literal["connecting", "active", "reconciling", "disconnected"] = Field(
        sa_type=Text
    )
    command_cursor: int = Field(default=0, ge=0)
    event_send_cursor: int = Field(default=0, ge=0)
    event_ack_sequence: int = Field(default=0, ge=0)
    connected_at_ms: Optional[UnixMilliseconds] = None
    disconnected_at_ms: Optional[UnixMilliseconds] = None
    last_seen_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_session(self) -> "BackendSessionRecord":
        if self.event_ack_sequence > self.event_send_cursor:
            raise ValueError("event ACK cannot exceed send cursor")
        if (self.state == "disconnected") != (self.disconnected_at_ms is not None):
            raise ValueError("session state and disconnected_at_ms must agree")
        return self


class ExecutorEndpointRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "executor_endpoint"

    endpoint_uuid: NonEmptyStr = Field(primary_key=True)
    transport: Transport
    host_uuid: NonEmptyStr
    instance_name: NonEmptyStr
    authority_epoch: NonEmptyStr
    adapter_epoch: Optional[NonEmptyStr] = None
    adapter_event_cursor: int = Field(default=0, ge=0)
    reconciliation_generation: int = Field(default=0, ge=0)
    state: Literal["online", "offline", "reconciling"] = Field(sa_type=Text)
    device_routes: List[DeviceRoute] = Field(
        default_factory=list,
        sa_column=json_text_column("device_routes_json", default_json="[]"),
    )
    action_capabilities: List[DeviceActionCapability] = Field(
        default_factory=list,
        sa_column=json_text_column("action_capabilities_json", default_json="[]"),
    )
    config: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("config_json", default_json="{}"),
    )
    snapshot_hash: str = ""
    registered_at_ms: UnixMilliseconds
    last_seen_at_ms: UnixMilliseconds
    reconciled_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1


class CommandInboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "command_inbox"

    command_uuid: NonEmptyStr = Field(primary_key=True)
    session_uuid: NonEmptyStr
    backend_sequence: int = Field(ge=1)
    command_type: Literal[
        "execute_job",
        "cancel_job",
        "release_failed",
        "replace_result",
        "resume_pending",
        "inventory_apply",
        "reconcile",
    ] = Field(sa_type=Text)
    job_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr
    command_fingerprint: NonEmptyStr
    summary: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("summary_json", default_json="{}"),
    )
    traceparent: Optional[str] = None
    status: Literal["received", "applying", "applied", "rejected"] = Field(
        sa_type=Text
    )
    received_at_ms: UnixMilliseconds
    applied_at_ms: Optional[UnixMilliseconds] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_command(self) -> "CommandInboxRecord":
        requires_job = self.command_type in {
            "execute_job",
            "cancel_job",
            "release_failed",
            "replace_result",
            "resume_pending",
        }
        if requires_job != (self.job_uuid is not None):
            raise ValueError("command_type and job_uuid do not agree")
        terminal = self.status in {"applied", "rejected"}
        if terminal != (self.applied_at_ms is not None):
            raise ValueError("command status and applied_at_ms must agree")
        return self


#: attempt 为何产生：首次派发 / 决策链 retry / 重启恢复 / 循环下一轮。
AttemptTrigger = Literal["initial", "retry_decision", "recovery", "loop_iteration"]


def validate_attempt_link(
    retry_of_job_uuid: Optional[str], attempt_no: int, attempt_trigger: str
) -> None:
    """attempt 1 没有重试链；attempt > 1 要么是重试（带 ``retry_of_job_uuid``），要么是循环下一轮。"""

    if retry_of_job_uuid is not None and attempt_no == 1:
        raise ValueError("retry link and attempt number must agree")
    if retry_of_job_uuid is None and attempt_no > 1 and attempt_trigger != "loop_iteration":
        raise ValueError("retry link and attempt number must agree")


class ExecutionJobRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "execution_job"

    job_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    attempt_group_uuid: NonEmptyStr
    retry_of_job_uuid: Optional[NonEmptyStr] = None
    attempt_no: int = Field(default=1, ge=1)
    attempt_trigger: AttemptTrigger = Field(default="initial", sa_type=Text)
    execute_command_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_payload_uuid: NonEmptyStr
    route_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    # SQLModel 0.0.x 无法从 Optional[Annotated[Literal, Field]] 推断列类型。
    transport: Optional[Transport] = Field(default=None, sa_type=Text)
    material_bindings: List[MaterialBinding] = Field(
        default_factory=list,
        sa_column=json_text_column("material_bindings_json", default_json="[]"),
    )
    scheduler_revision: int = Field(ge=0)
    scheduler_status_version: int = Field(default=0, ge=0)
    status: Literal[
        "accepted",
        "dispatch_pending",
        "dispatched",
        "running",
        "failure_waiting",
        "terminal_waiting",
        "succeeded",
        "failed",
        "canceled",
        "execution_unknown",
        "rejected",
    ] = Field(sa_type=Text)
    feedback_sequence: int = Field(default=0, ge=0)
    job_access_token_ciphertext: Optional[bytes] = Field(
        default=None,
        exclude=True,
        repr=False,
        sa_column=Column(LargeBinary, nullable=True),
    )
    token_key_id: Optional[str] = Field(
        default=None,
        exclude=True,
        repr=False,
        sa_column=Column(Text, nullable=True),
    )
    result_uuid: Optional[NonEmptyStr] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    terminal_gate_state: Literal[
        "none",
        "waiting_backend",
        "backend_confirmed",
        "released_failed",
        "result_replaced",
        "canceled",
    ] = Field(default="none", sa_type=Text)
    terminal_error_uuid: Optional[NonEmptyStr] = None
    terminal_required_scheduler_revision: Optional[int] = Field(default=None, ge=0)
    terminal_confirmed_scheduler_revision: Optional[int] = Field(default=None, ge=0)
    terminal_request_event_uuid: Optional[NonEmptyStr] = None
    terminal_decision_command_uuid: Optional[NonEmptyStr] = None
    terminal_decision: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("terminal_decision_json", default_json="{}"),
    )
    terminal_opened_at_ms: Optional[UnixMilliseconds] = None
    terminal_resolved_at_ms: Optional[UnixMilliseconds] = None
    accepted_at_ms: UnixMilliseconds
    dispatched_at_ms: Optional[UnixMilliseconds] = None
    started_at_ms: Optional[UnixMilliseconds] = None
    finished_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_job(self) -> "ExecutionJobRecord":
        validate_attempt_link(self.retry_of_job_uuid, self.attempt_no, self.attempt_trigger)
        route_values = (self.route_uuid, self.endpoint_uuid, self.transport)
        if any(value is None for value in route_values) and any(
            value is not None for value in route_values
        ):
            raise ValueError("route, endpoint and transport must be set together")
        terminal = self.status in {"succeeded", "failed", "canceled", "rejected"}
        if terminal != (self.finished_at_ms is not None):
            raise ValueError("job status and finished_at_ms must agree")
        gate_open = self.terminal_gate_state != "none"
        gate_identity = (
            self.terminal_error_uuid,
            self.terminal_request_event_uuid,
            self.terminal_opened_at_ms,
        )
        if gate_open != all(value is not None for value in gate_identity):
            raise ValueError("terminal gate state and identity fields must agree")
        resolved = self.terminal_gate_state in {
            "released_failed",
            "result_replaced",
            "canceled",
        }
        if resolved != (self.terminal_resolved_at_ms is not None):
            raise ValueError("terminal gate resolution fields must agree")
        return self


class AdapterCommandOutboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "adapter_command_outbox"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    adapter_command_uuid: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: NonEmptyStr
    source_command_uuid: Optional[NonEmptyStr] = None
    trigger_event_uuid: Optional[NonEmptyStr] = None
    target_adapter_epoch: Optional[NonEmptyStr] = None
    command_type: Literal[
        "execute",
        "cancel",
        "release_failed",
        "replace_result",
        "resume_pending",
        "reconcile_state",
    ] = Field(sa_type=Text)
    payload_uuid: Optional[NonEmptyStr] = None
    status: Literal["pending", "sent", "acknowledged", "failed"] = Field(
        sa_type=Text
    )
    delivery_attempt_count: int = Field(default=0, ge=0)
    created_at_ms: UnixMilliseconds
    available_at_ms: UnixMilliseconds = 0
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    ack_event_uuid: Optional[NonEmptyStr] = None
    last_error: Optional[str] = None


class AdapterEventInboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "adapter_event_inbox"

    adapter_event_uuid: NonEmptyStr = Field(primary_key=True)
    endpoint_uuid: NonEmptyStr
    adapter_epoch: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    adapter_command_uuid: Optional[NonEmptyStr] = None
    adapter_sequence: int = Field(ge=0)
    event_type: Literal[
        "accepted",
        "running",
        "feedback",
        "error_pending",
        "succeeded",
        "failed",
        "canceled",
        "endpoint_ready",
        "endpoint_snapshot",
        "endpoint_offline",
        "command_ack",
    ] = Field(sa_type=Text)
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr
    status: Literal["received", "processing", "processed", "rejected"] = Field(
        sa_type=Text
    )
    occurred_at_ms: Optional[UnixMilliseconds] = None
    received_at_ms: UnixMilliseconds
    processed_at_ms: Optional[UnixMilliseconds] = None
    error_message: Optional[str] = None


class BackendEventOutboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "backend_event_outbox"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    event_uuid: NonEmptyStr
    event_type: NonEmptyStr
    aggregate_type: NonEmptyStr
    aggregate_uuid: NonEmptyStr
    aggregate_version: PositiveVersion
    job_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("summary_json", default_json="{}"),
    )
    detail_payload_uuid: Optional[NonEmptyStr] = None
    traceparent: Optional[str] = None
    tracestate: Optional[str] = None
    status: Literal["pending", "sent", "acknowledged", "dead_letter"] = Field(
        sa_type=Text
    )
    created_at_ms: UnixMilliseconds
    available_at_ms: UnixMilliseconds
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    delivery_attempt_count: int = Field(default=0, ge=0)
    last_error: Optional[str] = None


DATA_TABLE_MODELS = (
    BackendSessionRecord,
    ExecutorEndpointRecord,
    CommandInboxRecord,
    ExecutionJobRecord,
    AdapterCommandOutboxRecord,
    AdapterEventInboxRecord,
    BackendEventOutboxRecord,
)


DATA_TABLES = (
    SCHEMA_IDENTITY_TABLE,
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
                'resume_pending','inventory_apply','reconcile'
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
                    'execute_job','cancel_job','release_failed','replace_result',
                    'resume_pending'
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
            attempt_trigger TEXT NOT NULL DEFAULT 'initial' CHECK (
                attempt_trigger IN ('initial','retry_decision','recovery','loop_iteration')
            ),
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
                OR (attempt_trigger = 'loop_iteration' AND retry_of_job_uuid IS NULL AND attempt_no > 1)
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
                'execute','cancel','release_failed','replace_result','resume_pending',
                'reconcile_state'
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

__all__ = [
    "AdapterCommandOutboxRecord",
    "AdapterEventInboxRecord",
    "BackendEventOutboxRecord",
    "BackendSessionRecord",
    "CommandInboxRecord",
    "DATA_TABLES",
    "DATA_TABLE_MODELS",
    "DeviceActionCapability",
    "DeviceRoute",
    "ExecutionJobRecord",
    "ExecutorEndpointRecord",
    "MaterialBinding",
    "Transport",
]
