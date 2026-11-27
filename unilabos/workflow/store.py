"""Backend 形状的进程内工作流定义与 SQLite 运行事实存储。"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)
from uuid import UUID, uuid4

from unilabos.persistence import open_database
from unilabos.persistence.inspection import table_names
from unilabos.workflow import source_bootstrap
from unilabos.workflow.authoring_candidate_hash import (
    AuthoringCandidateHashError,
    compute_authoring_candidate_hash,
)
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.event_writer import (
    append_frontend_event,
    append_runtime_event,
)
from unilabos.workflow.frozen_json_cache import decode_large_json_text
from unilabos.workflow.graph_validation import (
    CodedGraphValidationError,
    GraphValidationError,
    MissingTemplateError,
    validate_graph,
)
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.manual_action_review import (
    MANUAL_REVIEW_BASIS_FIELDS,
    MANUAL_REVIEW_JSON_FIELDS,
    manual_review_projection,
)
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
    WorkflowTaskPriority,
)
from unilabos.workflow.store_migrations import (
    drop_empty_workflow_definition_tables,
    ensure_device_action_run_schema,
    ensure_ephemeral_workflow_reference_schema,
    ensure_execution_lock_schema,
    ensure_local_cancellation_schema,
    ensure_station_task_submission_schema,
    ensure_task_material_admission_schema,
    ensure_task_resource_unlock_command_schema,
    ensure_workflow_inventory_schema,
    ensure_workflow_runtime_journal_schema,
    ensure_workflow_task_control_schema,
    ensure_workflow_task_debug_configuration_schema,
)

if TYPE_CHECKING:
    from unilabos.workflow.task_input import PreparedTaskInput

_STORE_INITIALIZATION_BUSY_TIMEOUT_SECONDS = 5.0
_STORE_INITIALIZATION_SQLITE_BUSY_TIMEOUT_MS = 100
_STORE_INITIALIZATION_RETRY_INTERVAL_SECONDS = 0.01
_STORE_SQLITE_BUSY_TIMEOUT_MS = 5000

# 任务删除是对已完成运行事实的展示清理，资格必须在同一个 SQLite
# 事务内根据这些持久状态重新核验。不要把前端投影中的状态当作安全门禁。
_TASK_DELETE_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "canceled", "timeout"}
)
_JOB_DELETE_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "skipped", "canceled", "timeout"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _decode_text(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


def _load(value: Optional[str], fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    # 冻结执行计划/工作流快照这类大字段按内容缓存解码结果（见 frozen_json_cache）。
    return decode_large_json_text(value, _decode_text)


def _collect_resource_slot_uuids(
    schema: Any,
    value: Any,
    output: set[str],
) -> None:
    """按冻结输入 Schema 收集物料占位符（ResourceSlot）的稳定身份。"""

    if not isinstance(schema, Mapping) or value is None:
        return
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            _collect_resource_slot_uuids(member, value, output)
        return
    if schema.get("$slot") == "ResourceSlot":
        if isinstance(value, Mapping):
            material_uuid = value.get("uuid")
            if isinstance(material_uuid, str) and material_uuid:
                output.add(material_uuid)
        return
    if schema.get("type") == "array":
        if isinstance(value, list):
            for item in value:
                _collect_resource_slot_uuids(schema.get("items"), item, output)
        return
    properties = schema.get("properties")
    if isinstance(value, Mapping) and isinstance(properties, Mapping):
        for name, child_schema in properties.items():
            _collect_resource_slot_uuids(child_schema, value.get(name), output)


def _task_input_material_uuids(
    contract_parameters: Any,
    task_input: Any,
) -> List[str]:
    """从冻结任务输入合同生成紧凑展示所需的物料 UUID 列表。"""

    if not isinstance(contract_parameters, list) or not isinstance(task_input, Mapping):
        return []
    result: set[str] = set()
    for parameter in contract_parameters:
        if not isinstance(parameter, Mapping):
            continue
        name = parameter.get("name")
        if not isinstance(name, str) or name not in task_input:
            continue
        _collect_resource_slot_uuids(
            parameter.get("schema"),
            task_input[name],
            result,
        )
    return sorted(result)


def _stored_task_priority(value: Any) -> str | float:
    """规范工作流任务（WorkflowTask）落库值并兼容旧的数值优先级。

    参数：``value`` 是 SQLite 中的 ``workflow_task.priority`` 原始值，或创建
    任务时传入的字符串枚举、旧数值权重。返回：``normal``/``high`` 字符串，或
    旧调用方使用的有限浮点数。异常：非法字符串或非有限数值抛出
    ``StoreConflict``，避免把不可排序的值写入任务事实。
    """

    if isinstance(value, WorkflowTaskPriority):
        return value.value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {
            WorkflowTaskPriority.NORMAL.value,
            WorkflowTaskPriority.HIGH.value,
        }:
            return normalized
        try:
            numeric = float(normalized)
        except (TypeError, ValueError):
            raise StoreConflict("工作流任务优先级必须是 normal 或 high") from None
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise StoreConflict("工作流任务优先级格式无效") from None
    if not math.isfinite(numeric):
        raise StoreConflict("工作流任务优先级必须是有限值")
    return numeric


class StoreNotFound(LookupError):
    pass


class StoreConflict(RuntimeError):
    pass


class StoreAdmissionRejected(StoreConflict):
    """输入/库存准入明确拒绝，创建事务未提交；不代表设备执行失败。"""


class StoreRevisionConflict(StoreConflict):
    pass


class StoreAuthoringConflict(StoreConflict):
    """Apply 事务提交前发生了 Authoring 前置条件冲突。

    ``message`` 是可直接展示给用户的具体原因；为空时由服务层使用错误码默认提示。
    """

    def __init__(self, code: str, message: str | None = None):
        super().__init__(code)
        self.code = code
        self.message = message


# JSON 导入和公共 Graph PUT 创建新节点时允许穿过保留元数据保护的创作语义。
# 执行器绑定仍由服务端维护；组合展开元数据必须随导出图导入，否则嵌套
# 条件/循环的边界映射和必填 Handle 会在图校验阶段失败。
_PUBLIC_CREATE_UNILAB_OBJECT_FIELDS = frozenset(
    {
        "input_bindings",
        "carry_bindings",
        "resource_refs",
        "site_group_bindings",
        "composite",
    }
)
_PUBLIC_CREATE_UNILAB_SCALAR_FIELDS = frozenset(
    {
        "control_region_kind",
        "authoring_result_name",
        "presentation_group",
        "parallel_scope",
        "parallel_order",
    }
)


def _public_create_unilab(submitted_unilab: Mapping[str, Any]) -> Dict[str, Any]:
    """从新节点提交中取出允许写入的创作语义 ``unilab`` 字段。

    参数：``submitted_unilab`` 是调用方提交的节点 ``meta_data.unilab``。
    返回：可写入权威图的公开子集；没有合法字段时为空对象。异常：无。
    """

    public_unilab: Dict[str, Any] = {}
    for field in _PUBLIC_CREATE_UNILAB_OBJECT_FIELDS:
        value = submitted_unilab.get(field)
        if isinstance(value, Mapping):
            public_unilab[field] = deepcopy(dict(value))
    for field in _PUBLIC_CREATE_UNILAB_SCALAR_FIELDS:
        if field not in submitted_unilab:
            continue
        public_unilab[field] = deepcopy(submitted_unilab[field])
    source_order = submitted_unilab.get("authoring_source_order")
    if (
        isinstance(source_order, int)
        and not isinstance(source_order, bool)
        and source_order >= 0
    ):
        public_unilab["authoring_source_order"] = source_order
    return public_unilab


class TemplateSnapshotProvider(Protocol):
    """提供最近一次完整设备与动作内存目录的窄接口。"""

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回一个不可变且可供整次 Store 操作复用的目录快照。"""


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workflow (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    name TEXT NOT NULL,
    tags TEXT NOT NULL,
    workflow_type TEXT NOT NULL DEFAULT 'normal'
        CHECK (workflow_type IN ('normal', 'experiment_operation')),
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS workflow_node_template (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    resource_template_uuid TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    class TEXT,
    goal TEXT NOT NULL,
    goal_default TEXT NOT NULL,
    feedback TEXT NOT NULL,
    result TEXT NOT NULL,
    schema TEXT,
    type TEXT NOT NULL,
    icon TEXT,
    header TEXT,
    footer TEXT,
    node_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_workflow_node_template_authority
    ON workflow_node_template(authority_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_template_active_business_key
    ON workflow_node_template(resource_template_uuid, name)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS workflow_handle_template (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    workflow_node_template_uuid TEXT NOT NULL,
    handle_key TEXT NOT NULL,
    io_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    type TEXT NOT NULL,
    required INTEGER NOT NULL,
    data_source TEXT,
    data_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_workflow_handle_template_node
    ON workflow_handle_template(workflow_node_template_uuid);
CREATE INDEX IF NOT EXISTS ix_workflow_handle_template_authority
    ON workflow_handle_template(authority_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_handle_template_active_business_key
    ON workflow_handle_template(
        workflow_node_template_uuid,
        handle_key,
        io_type
    )
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS workflow_node (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    workflow_uuid TEXT NOT NULL,
    workflow_node_template_uuid TEXT,
    parent_uuid TEXT,
    material_uuid TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    type TEXT NOT NULL,
    icon TEXT,
    pose TEXT NOT NULL,
    param TEXT NOT NULL,
    manual_confirmation TEXT NOT NULL DEFAULT '{}',
    footer TEXT,
    action_name TEXT,
    action_type TEXT,
    execution_policy TEXT NOT NULL,
    disabled INTEGER NOT NULL,
    minimized INTEGER NOT NULL,
    script TEXT,
    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
);
CREATE INDEX IF NOT EXISTS ix_workflow_node_workflow
    ON workflow_node(workflow_uuid);

CREATE TABLE IF NOT EXISTS workflow_edge (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    workflow_uuid TEXT NOT NULL,
    source_node_uuid TEXT NOT NULL,
    target_node_uuid TEXT NOT NULL,
    source_handle_uuid TEXT NOT NULL,
    target_handle_uuid TEXT NOT NULL,
    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
);
CREATE INDEX IF NOT EXISTS ix_workflow_edge_workflow
    ON workflow_edge(workflow_uuid);

CREATE TABLE IF NOT EXISTS workflow_task (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    workflow_uuid TEXT NOT NULL,
    status TEXT NOT NULL,
    workflow_snapshot TEXT NOT NULL,
    execution_plan TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'normal'
        CHECK (execution_mode IN ('normal', 'switching_to_step', 'step')),
    dispatch_gate TEXT NOT NULL DEFAULT 'active'
        CHECK (dispatch_gate IN ('active', 'paused')),
    target_node_uuid TEXT,
    control_status TEXT NOT NULL,
    cleanup_status TEXT NOT NULL,
    trace_context TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT NOT NULL,
    error_info TEXT NOT NULL,
    timeout_at TEXT,
    attention_reason TEXT,
    terminal_ghost_detected_at TEXT,
    reconciliation_resume_control_status TEXT,
    started_at TEXT,
    finished_at TEXT,
    debug_create_time TEXT,
    debug_update_time TEXT,
    debug_start_node_uuids TEXT,
    debug_breakpoint_node_uuids TEXT,
    debug_execution_policy TEXT CHECK (
        debug_execution_policy IS NULL
        OR debug_execution_policy IN ('step', 'continue')
    ),
    debug_status TEXT CHECK (
        debug_status IS NULL
        OR debug_status IN ('paused', 'running', 'completed', 'stopped')
    )
);
CREATE INDEX IF NOT EXISTS ix_workflow_task_workflow
    ON workflow_task(workflow_uuid);
CREATE INDEX IF NOT EXISTS ix_workflow_task_status
    ON workflow_task(status);

CREATE TABLE IF NOT EXISTS workflow_task_command (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    workflow_task_uuid TEXT NOT NULL,
    type TEXT NOT NULL CHECK (
        type IN ('step', 'switch_to_step', 'pause', 'resume', 'cancel', 'unlock_resources')
    ),
    target_node_uuid TEXT,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'rejected')),
    result TEXT NOT NULL,
    trace_context TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_command_idempotency_active
    ON workflow_task_command(workflow_task_uuid, idempotency_key)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_workflow_task_command_pending
    ON workflow_task_command(workflow_task_uuid, create_time, uuid)
    WHERE deleted_at IS NULL AND status = 'pending';

CREATE TABLE IF NOT EXISTS workflow_node_admission_hold (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    workflow_task_uuid TEXT NOT NULL,
    workflow_node_job_uuid TEXT NOT NULL,
    workflow_node_uuid TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('start', 'breakpoint', 'step')),
    status TEXT NOT NULL CHECK (status IN ('open', 'released', 'canceled')),
    released_at TEXT,
    FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid) ON DELETE CASCADE,
    FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_admission_hold_open
    ON workflow_node_admission_hold(workflow_task_uuid)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS workflow_task_debug_command (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    workflow_task_uuid TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('step', 'continue')),
    hold_uuid TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'rejected')),
    result TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid) ON DELETE CASCADE,
    FOREIGN KEY(hold_uuid) REFERENCES workflow_node_admission_hold(uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_debug_command_idempotency
    ON workflow_task_debug_command(workflow_task_uuid, idempotency_key);

CREATE TABLE IF NOT EXISTS workflow_node_job (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL,
    workflow_task_uuid TEXT NOT NULL,
    workflow_node_uuid TEXT NOT NULL,
    material_uuid TEXT,
    edge_agent_uuid TEXT,
    edge_command_uuid TEXT,
    job_access_token_hash TEXT NOT NULL DEFAULT '',
    feedback_sequence INTEGER NOT NULL,
    topological_index INTEGER NOT NULL,
    executor_kind TEXT NOT NULL,
    execution_policy TEXT NOT NULL,
    execution_timeout_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    param TEXT NOT NULL,
    feedback_data TEXT NOT NULL,
    return_info TEXT NOT NULL,
    control_data TEXT NOT NULL,
    error_info TEXT NOT NULL,
    dispatch_deadline_at TEXT,
    execution_deadline_at TEXT,
    cancel_command_uuid TEXT,
    cancel_ack_deadline_at TEXT,
    cancel_complete_deadline_at TEXT,
    cancel_accepted_at TEXT,
    uncertainty_reason TEXT,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
);
CREATE INDEX IF NOT EXISTS ix_workflow_node_job_task
    ON workflow_node_job(workflow_task_uuid);
CREATE INDEX IF NOT EXISTS ix_workflow_node_job_node
    ON workflow_node_job(workflow_node_uuid);

CREATE TABLE IF NOT EXISTS workflow_source_registration (
    workflow_uuid TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    package_root TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_source_registration_path
    ON workflow_source_registration(package_root, relative_path);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_source_registration_uri
    ON workflow_source_registration(source_uri);

CREATE TABLE IF NOT EXISTS workflow_authoring (
    workflow_uuid TEXT PRIMARY KEY,
    observed_draft_hash TEXT,
    draft_update_time TEXT,
    diagnostics TEXT NOT NULL,
    candidate_hash TEXT,
    candidate TEXT,
    applied_source TEXT,
    writeback_status TEXT NOT NULL DEFAULT 'settled',
    writeback_source TEXT,
    writeback_expected_hash TEXT,
    writeback_generation TEXT,
    update_time TEXT NOT NULL,
    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
);

CREATE TABLE IF NOT EXISTS frontend_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    data TEXT NOT NULL,
    create_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runtime_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_task_uuid TEXT NOT NULL,
    workflow_node_job_uuid TEXT,
    workflow_task_command_uuid TEXT,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'task_transition',
            'job_transition',
            'command_consumed',
            'feedback_committed',
            'uncertainty_opened',
            'uncertainty_resolved',
            'lock_operator_released',
            'startup_recovered'
        )
    ),
    from_status TEXT,
    to_status TEXT,
    data TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(data) AND json_type(data) = 'object'),
    create_time TEXT NOT NULL,
    FOREIGN KEY(workflow_task_uuid)
        REFERENCES workflow_task(uuid) ON DELETE CASCADE,
    FOREIGN KEY(workflow_node_job_uuid)
        REFERENCES workflow_node_job(uuid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_workflow_runtime_journal_task_sequence
    ON workflow_runtime_journal(workflow_task_uuid, sequence);
CREATE INDEX IF NOT EXISTS ix_workflow_runtime_journal_job_sequence
    ON workflow_runtime_journal(workflow_node_job_uuid, sequence);
"""


class WorkflowStore:
    """由单一连接持有的工作流定义目录或持久运行事实库。

    定义角色使用 ``:memory:``，运行角色使用文件 SQLite；两者复用相同事务与
    投影代码但不形成双权威。Store 方法用进程内可重入锁串行化事务，Workflow
    专属编排锁由 ``WorkflowService`` 持有。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        template_snapshot_provider: TemplateSnapshotProvider | None = None,
        persist_workflow_definitions: bool = True,
    ) -> None:
        """建立工作流持久事实存储并绑定可选内存模板目录。

        参数：``db_path`` 是工作流 SQLite 路径；提供
        ``template_snapshot_provider`` 时，所有模板读取只使用其不可变快照且不
        回退模板表；省略时保留隔离测试和遗留调用的 SQLite 模板适配器。
        ``persist_workflow_definitions=False`` 表示该文件连接只持有 Task/Job 等
        运行事实，并解除 Task 对可消失工作流定义的外键依赖。
        """

        initialization_deadline = (
            monotonic() + _STORE_INITIALIZATION_BUSY_TIMEOUT_SECONDS
        )
        self.path = str(db_path)
        self._template_snapshot_provider = template_snapshot_provider
        self._persist_workflow_definitions = persist_workflow_definitions
        self._lock = threading.RLock()
        self.db = open_database(
            self.path, "workflow", create_parent=True,
            transient=self.path == ":memory:" and persist_workflow_definitions,
        )
        self._conn = self.db
        self._conn.row_factory = sqlite3.Row
        self.db.initialize(
            lambda: self._initialize_sqlite(initialization_deadline),
            persist_workflow_definitions=persist_workflow_definitions,
        )
        from unilabos.persistence.resource_changes import initialize_resource_changes

        with self.transaction() as connection:
            initialize_resource_changes(connection, "workflow")
            from unilabos.workflow.task_read_model import initialize_task_read_model

            initialize_task_read_model(connection)
        # SQLite/PG 共用的既有中心账本迁移必须先于 composition 的任务重启恢复。
        # 未启用中心且没有历史中心事实时不创建任何中心表，也不导入网络客户端。
        from unilabos.center_protocol.events import EventEnvelopeError
        from unilabos.persistence.center_schema import CenterSchemaError, ensure_center_schema

        try:
            with self.transaction() as connection:
                from unilabos.persistence.recovery_mode import evidence_only

                ensure_center_schema(connection, allow_event_gaps=evidence_only(connection))
        except (CenterSchemaError, EventEnvelopeError) as error:
            self.db.close()
            raise StoreConflict(str(error)) from error
        except BaseException:
            self.db.close()
            raise

    def _initialize_sqlite(self, initialization_deadline: float) -> None:
        try:
            # SQLite 有界 busy 重试统一覆盖线程与进程间的 WAL/schema 竞争，
            # 不用进程全局锁阻塞无关数据库。
            with self._lock:
                initialization_busy_timeout_ms = (
                    _STORE_INITIALIZATION_SQLITE_BUSY_TIMEOUT_MS
                )
                self._conn.execute(
                    f"PRAGMA busy_timeout = {initialization_busy_timeout_ms}"
                )
                self._retry_initialization(
                    lambda: self._conn.execute("PRAGMA journal_mode = WAL"),
                    deadline=initialization_deadline,
                )
                self._retry_initialization(
                    lambda: self._conn.execute("PRAGMA synchronous = NORMAL"),
                    deadline=initialization_deadline,
                )
                self._retry_initialization(
                    lambda: self._conn.executescript(_SCHEMA),
                    deadline=initialization_deadline,
                )
                self._retry_initialization(
                    lambda: self._conn.execute("BEGIN IMMEDIATE"),
                    deadline=initialization_deadline,
                )
                try:
                    if self._persist_workflow_definitions:
                        workflow_columns = {
                            row["name"]
                            for row in self._conn.execute(
                                "PRAGMA table_info(workflow)"
                            ).fetchall()
                        }
                        if "workflow_type" not in workflow_columns:
                            self._conn.execute(
                                """
                                ALTER TABLE workflow
                                ADD COLUMN workflow_type TEXT NOT NULL DEFAULT 'normal'
                                    CHECK (
                                        workflow_type IN (
                                            'normal',
                                            'experiment_operation'
                                        )
                                    )
                                """
                            )
                    workflow_node_columns = {
                        row["name"]
                        for row in self._conn.execute(
                            "PRAGMA table_info(workflow_node)"
                        ).fetchall()
                    }
                    if "manual_confirmation" not in workflow_node_columns:
                        self._conn.execute(
                            """
                            ALTER TABLE workflow_node
                            ADD COLUMN manual_confirmation TEXT NOT NULL DEFAULT '{}'
                            """
                        )
                    ensure_device_action_run_schema(self._conn)
                    ensure_station_task_submission_schema(self._conn)
                    ensure_workflow_task_control_schema(self._conn)
                    ensure_workflow_task_debug_configuration_schema(self._conn)
                    ensure_task_resource_unlock_command_schema(self._conn)
                    from unilabos.workflow.workflow_boundary import (
                        ensure_workflow_boundary_schema,
                    )

                    ensure_workflow_boundary_schema(self._conn)
                    from unilabos.workflow.station_event_outbox import (
                        ensure_station_event_outbox_schema,
                    )

                    ensure_station_event_outbox_schema(self._conn)
                    if not self._persist_workflow_definitions:
                        ensure_ephemeral_workflow_reference_schema(self._conn)
                    ensure_task_material_admission_schema(self._conn)
                    ensure_workflow_runtime_journal_schema(self._conn)
                    ensure_execution_lock_schema(self._conn)
                    ensure_local_cancellation_schema(self._conn)
                    ensure_workflow_inventory_schema(self._conn)
                    from unilabos.workflow.error_control import ensure_error_control_schema

                    ensure_error_control_schema(self._conn)
                    if not self._persist_workflow_definitions:
                        drop_empty_workflow_definition_tables(self._conn)
                    if self._persist_workflow_definitions:
                        columns = {
                            row["name"]
                            for row in self._conn.execute(
                                "PRAGMA table_info(workflow_authoring)"
                            ).fetchall()
                        }
                        if "writeback_generation" not in columns:
                            self._conn.execute(
                                """
                                ALTER TABLE workflow_authoring
                                ADD COLUMN writeback_generation TEXT
                                """
                            )
                        legacy_markers = self._conn.execute(
                            """
                            SELECT workflow_uuid
                            FROM workflow_authoring
                            WHERE writeback_status = 'pending'
                              AND writeback_source IS NOT NULL
                              AND writeback_expected_hash IS NOT NULL
                              AND writeback_generation IS NULL
                            """,
                        ).fetchall()
                        for marker in legacy_markers:
                            self._conn.execute(
                                """
                                UPDATE workflow_authoring
                                SET writeback_generation = ?
                                WHERE workflow_uuid = ?
                                  AND writeback_status = 'pending'
                                  AND writeback_source IS NOT NULL
                                  AND writeback_expected_hash IS NOT NULL
                                  AND writeback_generation IS NULL
                                """,
                                (str(uuid4()), marker["workflow_uuid"]),
                            )
                except BaseException:
                    self._conn.rollback()
                    raise
                else:
                    self._conn.commit()
                    self._conn.execute(
                        f"PRAGMA busy_timeout = {_STORE_SQLITE_BUSY_TIMEOUT_MS}"
                    )
        except BaseException:
            self._conn.close()
            raise

    def _retry_initialization(
        self,
        operation: Callable[[], object],
        *,
        deadline: float,
    ) -> None:
        while True:
            try:
                operation()
                return
            except sqlite3.OperationalError as error:
                error_code = getattr(error, "sqlite_errorcode", None)
                base_error_code = (
                    error_code & 0xFF if isinstance(error_code, int) else None
                )
                busy_message = str(error).lower() in {
                    "database is locked",
                    "database table is locked",
                }
                if (
                    base_error_code
                    not in {
                        sqlite3.SQLITE_BUSY,
                        sqlite3.SQLITE_LOCKED,
                    }
                    and not busy_message
                ):
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise
                self._conn.rollback()
                sleep(
                    min(
                        _STORE_INITIALIZATION_RETRY_INTERVAL_SECONDS,
                        remaining,
                    )
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """在存储锁保护下提供只读连接视图。

        参数：无。返回：一个仅供当前 ``with`` 作用域查询的 SQLite 连接；调用方
        不得提交、回滚或保留该连接。异常：查询产生的 SQLite 异常原样传播。

        该入口供与工作流库同部署的持久化扩展读取自己的表，避免扩展依赖
        ``WorkflowStore`` 的锁和连接私有字段；需要写入时必须改用
        :meth:`transaction`。
        """

        with self._lock:
            yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # Workflow 与 Graph --------------------------------------------------

    def create_workflow(
        self,
        *,
        workflow_uuid: str,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        workflow_type: str = "normal",
    ) -> Dict[str, Any]:
        """在定义目录创建一个空工作流。

        参数：稳定 UUID、名称、标签、描述、公开元数据和已校验工作流类型构成
        首版定义。返回：修订为 1 的完整工作流投影。异常：UUID 冲突或类型约束
        失败时抛出 ``StoreConflict``；事务失败不留下半条定义。
        """

        now = utc_now()
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO workflow(
                        uuid, create_time, update_time, deleted_at,
                        description, meta_data, name, tags, workflow_type,
                        revision
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        workflow_uuid,
                        now,
                        now,
                        description,
                        _json(meta_data),
                        name,
                        _json(tags),
                        workflow_type,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"workflow {workflow_uuid} already exists") from exc
        return self.get_workflow(workflow_uuid)

    def create_workflow_with_graph(
        self,
        *,
        workflow_uuid: str,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        inventory_requirements: Optional[
            List[WorkflowInventoryRequirementWrite]
        ] = None,
        workflow_type: str = "normal",
        node_templates: List[Dict[str, Any]] | None = None,
        handle_templates: List[Dict[str, Any]] | None = None,
        template_catalog_fingerprint: str | None = None,
        trusted_authoring_graph: bool = False,
    ) -> Dict[str, Any]:
        """在一个事务中创建工作流及其首版完整图。

        参数：工作流字段及 ``workflow_type`` 构成新定义，``nodes``/``edges``
        已使用新身份重建引用；
        ``node_templates``/``handle_templates`` 是 AST 编译候选实际引用的目录子集，
        与工作流图在同一事务内校验或投影；``trusted_authoring_graph`` 只允许 AST
        编译器生成的图保留系统创作元数据；普通复制和旧版导入仍禁止提交
        执行器绑定，但可写入输入绑定、循环 carry、控制区域和组合调用边界等
        创作语义。
        返回修订为 1 的完整图；任何身份、模板或图语义错误都会回滚工作流主记录，
        因此复制/导入不会留下空壳工作流。
        """

        now = utc_now()
        try:
            with self.transaction() as conn:
                existing = conn.execute(
                    "SELECT deleted_at FROM workflow WHERE uuid = ?",
                    (workflow_uuid,),
                ).fetchone()
                if existing is not None:
                    # 被软删除的定义仍然是身份墓碑；只有定义目录被显式清空后，
                    # 导入才允许再次占用相同 UUID。
                    raise StoreConflict(f"workflow {workflow_uuid} already exists")
                else:
                    conn.execute(
                        """
                        INSERT INTO workflow(
                            uuid, create_time, update_time, deleted_at,
                            description, meta_data, name, tags, workflow_type,
                            revision
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            workflow_uuid,
                            now,
                            now,
                            description,
                            _json(meta_data),
                            name,
                            _json(tags),
                            workflow_type,
                        ),
                    )
                if node_templates is not None or handle_templates is not None:
                    if not template_catalog_fingerprint:
                        raise StoreConflict("Candidate Catalog 缺少目录指纹")
                    self._ensure_authoring_catalog_projection(
                        conn,
                        node_templates=node_templates or [],
                        handle_templates=handle_templates or [],
                        authority_id=template_catalog_fingerprint,
                        now=now,
                    )
                self._reconcile_graph(
                    conn,
                    workflow_uuid=workflow_uuid,
                    expected_revision=1,
                    nodes=nodes,
                    edges=edges,
                    inventory_requirements=inventory_requirements,
                    advance_revision=False,
                    protect_reserved_metadata=not trusted_authoring_graph,
                    semantic_workflow_meta_data=(
                        meta_data if trusted_authoring_graph else None
                    ),
                    validate_workflow_io_contract=True,
                )
                self._assert_graph_executable(conn, workflow_uuid)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"workflow {workflow_uuid} already exists") from exc
        return self.get_graph(workflow_uuid)

    def get_workflow(
        self,
        workflow_uuid: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        database = conn or self._conn
        with self._lock:
            row = database.execute(
                "SELECT * FROM workflow WHERE uuid = ? AND deleted_at IS NULL",
                (workflow_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow {workflow_uuid} not found")
        return self._workflow_row(row)

    def list_workflows(
        self,
        *,
        page: int,
        page_size: int,
        name: str = "",
        workflow_type: str | None = None,
        publication_status: str | None = None,
    ) -> Dict[str, Any]:
        """按名称、类型与当前发布状态分页读取工作流。

        参数：页码和页长确定结果窗口；``name`` 模糊匹配名称；两个可选筛选分别
        约束工作流类型及当前修订是否已有发布合同。返回：筛选后的总数与当前页。
        异常：发布状态筛选要求发布合同表已由调用方初始化，数据库错误原样传播。
        """

        where = "workflow.deleted_at IS NULL"
        values: List[Any] = []
        if name:
            where += " AND workflow.name LIKE ?"
            values.append(f"%{name}%")
        if workflow_type is not None:
            where += " AND workflow.workflow_type = ?"
            values.append(workflow_type)
        current_publication = """
            EXISTS (
                SELECT 1 FROM published_workflow_contract AS published
                WHERE published.workflow_uuid = workflow.uuid
                  AND published.workflow_revision = workflow.revision
                  AND published.deleted_at IS NULL
            )
        """
        if publication_status == "published":
            where += f" AND {current_publication}"
        elif publication_status == "source":
            where += f" AND NOT {current_publication}"
        offset = (page - 1) * page_size
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM workflow WHERE {where}",
                values,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT workflow.* FROM workflow WHERE {where}
                ORDER BY workflow.create_time DESC, workflow.uuid
                LIMIT ? OFFSET ?
                """,
                (*values, page_size, offset),
            ).fetchall()
        return {
            "items": [self._workflow_row(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def update_workflow(
        self,
        workflow_uuid: str,
        *,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        workflow_type: str,
    ) -> Dict[str, Any]:
        """更新工作流根字段但不改变图修订。

        参数：``workflow_uuid`` 定位定义，其余字段是已校验后的完整替换值；类型
        只能由服务层传入规范值。返回：更新后的工作流投影。异常：工作流不存在或
        数据库类型约束失败时原样抛出，事务整体回滚。
        """

        with self.transaction() as conn:
            self.get_workflow(workflow_uuid, conn=conn)
            conn.execute(
                """
                UPDATE workflow
                SET name = ?, tags = ?, description = ?, meta_data = ?,
                    workflow_type = ?, update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (
                    name,
                    _json(tags),
                    description,
                    _json(meta_data),
                    workflow_type,
                    utc_now(),
                    workflow_uuid,
                ),
            )
        return self.get_workflow(workflow_uuid)

    def delete_workflow(self, workflow_uuid: str, *, purge: bool = False) -> None:
        """删除工作流定义。

        默认保留软删除墓碑，供持久运行事实库及旧版调用方追溯；进程内定义
        目录可传 ``purge=True``，将定义、图和创作元数据一并硬删除。运行任务
        不在定义目录中，因此不会随 purge 被删除。
        """

        now = utc_now()
        with self.transaction() as conn:
            self.get_workflow(workflow_uuid, conn=conn)
            if purge:
                if conn.execute(
                    "SELECT 1 FROM workflow_task WHERE workflow_uuid = ? LIMIT 1",
                    (workflow_uuid,),
                ).fetchone() is not None:
                    raise StoreConflict(
                        f"workflow {workflow_uuid} has historical tasks"
                    )
                for table in (
                    "workflow_inventory_requirement",
                    "workflow_source_registration",
                    "workflow_authoring",
                    "published_workflow_contract",
                    "workflow_edge",
                    "workflow_node",
                ):
                    conn.execute(
                        f"DELETE FROM {table} WHERE workflow_uuid = ?",
                        (workflow_uuid,),
                    )
                conn.execute("DELETE FROM workflow WHERE uuid = ?", (workflow_uuid,))
                return
            conn.execute(
                "UPDATE workflow SET deleted_at = ?, update_time = ? WHERE uuid = ?",
                (now, now, workflow_uuid),
            )
            conn.execute(
                "UPDATE workflow_node SET deleted_at = ?, update_time = ? "
                "WHERE workflow_uuid = ? AND deleted_at IS NULL",
                (now, now, workflow_uuid),
            )
            conn.execute(
                "UPDATE workflow_edge SET deleted_at = ?, update_time = ? "
                "WHERE workflow_uuid = ? AND deleted_at IS NULL",
                (now, now, workflow_uuid),
            )

    def discard_uncommitted_workflow(self, workflow_uuid: str) -> None:
        """硬删除一次尚未对外成功的工作流定义创建。

        该方法只供同时持有定义目录与运行事实库的 Service 导入补偿使用；调用方
        必须先在工作流锁内证明运行库没有 Task。正常用户删除仍必须走软删除。
        """

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT uuid FROM workflow WHERE uuid = ?",
                (workflow_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow {workflow_uuid} not found")
            conn.execute(
                "DELETE FROM workflow_inventory_requirement WHERE workflow_uuid = ?",
                (workflow_uuid,),
            )
            conn.execute(
                "DELETE FROM workflow_source_registration WHERE workflow_uuid = ?",
                (workflow_uuid,),
            )
            conn.execute(
                "DELETE FROM workflow_authoring WHERE workflow_uuid = ?",
                (workflow_uuid,),
            )
            conn.execute(
                "DELETE FROM workflow_edge WHERE workflow_uuid = ?",
                (workflow_uuid,),
            )
            conn.execute(
                "DELETE FROM workflow_node WHERE workflow_uuid = ?",
                (workflow_uuid,),
            )
            conn.execute(
                "DELETE FROM workflow WHERE uuid = ?",
                (workflow_uuid,),
            )

    def has_workflow_tasks(self, workflow_uuid: str) -> bool:
        """返回运行事实库中是否已经存在该工作流创建的 Task。"""

        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM workflow_task WHERE workflow_uuid = ? LIMIT 1",
                    (workflow_uuid,),
                ).fetchone()
                is not None
            )

    def get_graph(
        self,
        workflow_uuid: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        database = conn or self._conn
        workflow = self.get_workflow(workflow_uuid, conn=database)
        with self._lock:
            node_rows = database.execute(
                """
                SELECT * FROM workflow_node
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                ORDER BY create_time, uuid
                """,
                (workflow_uuid,),
            ).fetchall()
            edge_rows = database.execute(
                """
                SELECT * FROM workflow_edge
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                ORDER BY create_time, uuid
                """,
                (workflow_uuid,),
            ).fetchall()
            inventory_requirement_rows = database.execute(
                """
                SELECT * FROM workflow_inventory_requirement
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                ORDER BY sort_order, uuid
                """,
                (workflow_uuid,),
            ).fetchall()
            template_uuids = [
                row["workflow_node_template_uuid"]
                for row in node_rows
                if row["workflow_node_template_uuid"]
            ]
            node_templates: List[Dict[str, Any]] = []
            handle_templates: List[Dict[str, Any]] = []
            catalog_snapshot = (
                self._template_snapshot_provider.snapshot()
                if template_uuids and self._template_snapshot_provider is not None
                else None
            )
            if template_uuids:
                if catalog_snapshot is not None:
                    node_templates, handle_templates = self._catalog_entities(
                        template_uuids,
                        snapshot=catalog_snapshot,
                    )
                else:
                    marks = ",".join("?" for _ in template_uuids)
                    template_rows = database.execute(
                        f"""
                        SELECT * FROM workflow_node_template
                        WHERE uuid IN ({marks}) AND deleted_at IS NULL
                        ORDER BY create_time, uuid
                        """,
                        template_uuids,
                    ).fetchall()
                    handle_rows = database.execute(
                        f"""
                        SELECT * FROM workflow_handle_template
                        WHERE workflow_node_template_uuid IN ({marks})
                          AND deleted_at IS NULL
                        ORDER BY create_time, uuid
                        """,
                        template_uuids,
                    ).fetchall()
                    node_templates = [
                        self._node_template_row(row) for row in template_rows
                    ]
                    handle_templates = [
                        self._handle_template_row(row) for row in handle_rows
                    ]
        return {
            "workflow": workflow,
            "nodes": [
                self._public_node_row(row, snapshot=catalog_snapshot)
                for row in node_rows
            ],
            "edges": [self._edge_row(row) for row in edge_rows],
            "inventory_requirements": [
                self._inventory_requirement_row(row)
                for row in inventory_requirement_rows
            ],
            "node_templates": node_templates,
            "handle_templates": handle_templates,
        }

    def _catalog_entities(
        self,
        template_uuids: Iterable[str],
        *,
        snapshot: AuthoringCatalogSnapshot | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从一个不可变目录快照分离指定模板及全部 Handle。

        参数：``template_uuids`` 是工作流图实际引用的模板身份；调用方可传入已经
        冻结的 ``snapshot`` 保证一次事务不混用代际。返回：按 UUID 排序的节点和
        Handle 普通字典；目录未装配或任一身份缺失时关闭式失败。
        """

        provider = self._template_snapshot_provider
        if provider is None:
            raise RuntimeError("工作流存储没有装配内存模板目录")
        current_snapshot = snapshot or provider.snapshot()
        actions = []
        for template_reference in sorted(set(template_uuids)):
            try:
                actions.append(current_snapshot.require_template(template_reference))
            except AuthoringCatalogError:
                try:
                    actions.append(
                        current_snapshot.require_template_key(template_reference)
                    )
                except AuthoringCatalogError as error:
                    # 已发布组合调用模板由发布存储生成，本来就不属于设备动作目录
                    # 快照；这里只解析其持久化投影，未知普通模板仍关闭式失败。
                    persisted = self._published_template_entities_for_reference(
                        template_reference
                    )
                    if persisted is None:
                        raise StoreNotFound(
                            f"workflow node template {template_reference} not found"
                        ) from error
                    actions.append(persisted)
        node_templates = []
        handle_templates = []
        for action in actions:
            if isinstance(action, tuple):
                node_template, handles = action
                node_templates.append(node_template)
                handle_templates.extend(handles)
            else:
                node_templates.append(action.detached_template())
                handle_templates.extend(action.detached_handles())
        node_templates.sort(key=lambda item: str(item["uuid"]))
        handle_templates.sort(key=lambda item: str(item["uuid"]))
        return node_templates, handle_templates

    def _published_template_entities(
        self,
        template_uuid: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """读取发布合同生成的节点模板及连接点投影。

        参数：``template_uuid`` 是发布合同固定的组合节点模板 UUID。返回：模板
        与其 Handle 投影；若该 UUID 不是发布合同投影则返回 ``None``。异常：仅
        传播 SQLite 读取错误。该接缝只补齐动作目录不包含的已发布组合模板。
        """

        row = self._conn.execute(
            """
            SELECT * FROM workflow_node_template
            WHERE uuid = ?
              AND deleted_at IS NULL
              AND authority_id LIKE 'published-workflow-contract:%'
            """,
            (template_uuid,),
        ).fetchone()
        if row is None:
            return None
        handles = self._conn.execute(
            """
            SELECT * FROM workflow_handle_template
            WHERE workflow_node_template_uuid = ? AND deleted_at IS NULL
            ORDER BY create_time, uuid
            """,
            (template_uuid,),
        ).fetchall()
        return (
            self._node_template_row(row),
            [self._handle_template_row(handle) for handle in handles],
        )

    def _published_template_entities_for_reference(
        self,
        reference: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """按外部 UUID 或历史名称键读取已发布组合模板。

        参数：``reference`` 是工作流节点保存的模板引用，既可能是发布合同固定的
        UUID，也可能是早期目录把组合模板转换成的 ``设备名.workflow:工作流 UUID``
        名称键。返回：模板及连接点投影；引用未知或不是组合名称键时返回
        ``None``。异常：SQLite 读取错误原样传播。
        """

        persisted = self._published_template_entities(reference)
        if persisted is not None:
            return persisted
        if not isinstance(reference, str) or ".workflow:" not in reference:
            return None
        _owner, workflow_uuid_text = reference.rsplit(".workflow:", 1)
        try:
            workflow_uuid = str(UUID(workflow_uuid_text))
        except (AttributeError, TypeError, ValueError):
            return None
        try:
            rows = self._conn.execute(
                """
                SELECT node_template_uuid
                FROM published_workflow_contract
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                ORDER BY version DESC, create_time DESC, uuid DESC
                """,
                (workflow_uuid,),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table: published_workflow_contract" not in str(error):
                raise
            return None
        for row in rows:
            persisted = self._published_template_entities(
                str(row["node_template_uuid"])
            )
            if persisted is not None:
                return persisted
        return None

    def _public_node_row(
        self,
        row: sqlite3.Row,
        *,
        snapshot: AuthoringCatalogSnapshot | None = None,
    ) -> Dict[str, Any]:
        """把内部名称模板引用恢复为既有外部 UUID 字段。

        参数：``row`` 是工作流节点持久行。返回：原 Backend-shaped 节点投影；
        SQLite 遗留适配器保持原值，内存目录模式严格解析名称键。
        """

        result = self._node_row(row)
        reference = result.get("workflow_node_template_uuid")
        if reference is None or self._template_snapshot_provider is None:
            return result
        current_snapshot = snapshot or self._template_snapshot_provider.snapshot()
        try:
            # 启动迁移前创建的节点可能已经保存外部 UUID；仍按同一目录验证。
            external_uuid = str(
                current_snapshot.require_template(reference).template["uuid"]
            )
        except AuthoringCatalogError:
            try:
                external_uuid = current_snapshot.template_uuid_for_key(reference)
            except AuthoringCatalogError as error:
                published_projection = self._published_template_entities_for_reference(
                    reference
                )
                if published_projection is None:
                    raise StoreNotFound(
                        f"workflow node template {reference} not found"
                    ) from error
                # 组合模板历史上使用 ``<device>.workflow:<workflow_uuid>`` 键
                # 持久化；子工作流发布后暴露稳定投影 UUID，使调用方可通过公共
                # 合同往返保存图。
                external_uuid = str(published_projection[0]["uuid"])
        result["workflow_node_template_uuid"] = external_uuid
        return result

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        """一次冻结工作流图与应用源码发布资格事实。

        参数：``workflow_uuid`` 是活动工作流（Workflow）稳定身份。返回：同一
        SQLite 锁视图中的完整图、已应用源码和原始草稿字节摘要
        ``source_draft_hash``；尚未应用时 ``applied_source`` 为 ``None``，草稿
        摘要也可能为空。组合目录应使用原始草稿摘要核对领域包发布目录，不能把
        ``applied_source.source_hash``（规范化源码摘要）当作同一证据。异常：
        工作流缺失或软删除时抛出 ``StoreNotFound``，持久 JSON 损坏等读取错误
        原样传播。
        """

        with self._lock:
            graph = self.get_graph(workflow_uuid, conn=self._conn)
            row = self._conn.execute(
                """
                SELECT observed_draft_hash, applied_source
                FROM workflow_authoring
                WHERE workflow_uuid = ?
                """,
                (workflow_uuid,),
            ).fetchone()
            applied_source = (
                _load(row["applied_source"], None) if row is not None else None
            )
            source_draft_hash = (
                row["observed_draft_hash"] if row is not None else None
            )
            return {
                **graph,
                "applied_source": applied_source,
                "source_draft_hash": source_draft_hash,
            }

    def list_published_template_projections(self) -> list[dict[str, Any]]:
        """返回当前进程已恢复的发布组合模板及连接点投影。

        参数：无。返回：每项包含发布合同 UUID、来源工作流修订、节点模板以及
        连接点模板；只读当前定义目录，不把运行事实或新的发布合同写入数据库。
        异常：SQLite 读取错误原样传播。该入口供模板目录重建时把跨重启恢复的
        发布模板身份重新并入编译目录，避免合同模板 UUID 与运行时投影脱节。
        """

        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT uuid, workflow_uuid, workflow_revision, node_template_uuid
                    FROM published_workflow_contract
                    WHERE deleted_at IS NULL
                    ORDER BY workflow_uuid, workflow_revision, version, uuid
                    """
                ).fetchall()
            except sqlite3.OperationalError as error:
                # The publication store creates this auxiliary table lazily. The
                # first catalog build can legitimately run before that store has
                # been initialized, in which case there is no overlay to apply.
                if "no such table: published_workflow_contract" not in str(error):
                    raise
                rows = []
            result: list[dict[str, Any]] = []
            for row in rows:
                projection = self._published_template_entities(
                    str(row["node_template_uuid"])
                )
                if projection is None:
                    continue
                template, handles = projection
                result.append(
                    {
                        "contract_uuid": str(row["uuid"]),
                        "workflow_uuid": str(row["workflow_uuid"]),
                        "workflow_revision": int(row["workflow_revision"]),
                        "node_template_uuid": str(row["node_template_uuid"]),
                        "template": template,
                        "handles": handles,
                    }
                )
            return result

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        inventory_requirements: Optional[
            List[WorkflowInventoryRequirementWrite]
        ] = None,
        protect_reserved_metadata: bool = False,
        validate_workflow_io_contract: bool = False,
        workflow_meta_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """事务性保存完整工作流图并返回最新投影。

        参数说明：`revision` 是乐观并发修订；`nodes/edges` 是完整替换集合；
        `protect_reserved_metadata` 保护服务端元数据，但 JSON 导入可写入组合
        调用边界；
        `validate_workflow_io_contract` 决定是否启用严格公共输入/输出合同；
        ``workflow_meta_data`` 可把可信根元数据与图替换放入同一事务。
        """

        with self.transaction() as conn:
            self._reconcile_graph(
                conn,
                workflow_uuid=workflow_uuid,
                expected_revision=revision,
                nodes=nodes,
                edges=edges,
                inventory_requirements=inventory_requirements,
                advance_revision=True,
                protect_reserved_metadata=protect_reserved_metadata,
                semantic_workflow_meta_data=workflow_meta_data,
                validate_workflow_io_contract=validate_workflow_io_contract,
            )
            if workflow_meta_data is not None:
                conn.execute(
                    """
                    UPDATE workflow
                    SET meta_data = ?, update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (_json(workflow_meta_data), utc_now(), workflow_uuid),
                )
            self._assert_graph_executable(conn, workflow_uuid)
        return self.get_graph(workflow_uuid)

    def preview_graph_replacement(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        inventory_requirements: Optional[
            List[WorkflowInventoryRequirementWrite]
        ] = None,
        protect_reserved_metadata: bool = False,
        validate_workflow_io_contract: bool = False,
        workflow_meta_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """在回滚事务中构造完整图替换后的精确候选投影。

        参数与 :meth:`save_graph` 相同。返回：经过同一模板、身份、连线和工作流
        输入/输出合同校验，但仍保持当前修订号的完整图。异常：与真实保存完全
        一致。无论成功或失败，本方法都回滚全部节点、连线、事件和时间字段，供
        领域源码写回链在触碰内存权威前生成并验证规范 Python。
        """

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._reconcile_graph(
                    self._conn,
                    workflow_uuid=workflow_uuid,
                    expected_revision=revision,
                    nodes=nodes,
                    edges=edges,
                    inventory_requirements=inventory_requirements,
                    advance_revision=False,
                    protect_reserved_metadata=protect_reserved_metadata,
                    semantic_workflow_meta_data=workflow_meta_data,
                    validate_workflow_io_contract=validate_workflow_io_contract,
                )
                if workflow_meta_data is not None:
                    self._conn.execute(
                        """
                        UPDATE workflow
                        SET meta_data = ?, update_time = ?
                        WHERE uuid = ? AND deleted_at IS NULL
                        """,
                        (_json(workflow_meta_data), utc_now(), workflow_uuid),
                    )
                self._assert_graph_executable(self._conn, workflow_uuid)
                candidate = self.get_graph(workflow_uuid, conn=self._conn)
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.rollback()
        return candidate

    def _assert_graph_executable(
        self,
        conn: sqlite3.Connection,
        workflow_uuid: str,
    ) -> None:
        """用执行计划构建器与调度编译器校验事务内的候选图。

        保存只接受能生成执行计划的图：纯图结构规则只在计划层维护一份，
        保存、预检和建任务共用，避免“保存成功、运行才拒绝”并覆盖有效图。
        资源计划的工站绑定属于建任务时的事实，这里剥离后再编译。异常：不可
        执行时抛出带具体原因的 ``StoreAuthoringConflict``，由调用事务回滚。
        """

        from unilabos.workflow.graph_executability import (
            GraphNotExecutable,
            assert_graph_executable,
        )

        try:
            assert_graph_executable(
                self.get_graph(workflow_uuid, conn=conn), workflow_uuid=workflow_uuid
            )
        except GraphNotExecutable as error:
            raise StoreAuthoringConflict(error.code, error.message) from error

    def _reconcile_graph(
        self,
        conn: sqlite3.Connection,
        *,
        workflow_uuid: str,
        expected_revision: int,
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        inventory_requirements: Optional[
            List[WorkflowInventoryRequirementWrite]
        ] = None,
        advance_revision: bool,
        protect_reserved_metadata: bool = False,
        semantic_workflow_meta_data: Optional[Dict[str, Any]] = None,
        validate_workflow_io_contract: bool = False,
    ) -> int:
        """在现有事务中核对并写入完整工作流图。

        参数说明：``conn`` 是调用方持有的唯一 SQLite 写事务；``workflow_uuid``
        是工作流（Workflow）稳定身份；``expected_revision`` 是乐观并发预期修订；
        ``nodes`` 与 ``edges`` 是完整替换集合；``advance_revision`` 控制成功后是否
        推进修订；``protect_reserved_metadata`` 保留服务端私有元数据；
        ``semantic_workflow_meta_data`` 可替换本轮语义校验使用的工作流元数据；
        ``validate_workflow_io_contract`` 控制是否启用严格工作流输入/输出
        （Workflow I/O）合同。返回：本事务采用的最终工作流修订。异常：工作流
        不存在抛出 ``StoreNotFound``，修订不匹配抛出 ``StoreRevisionConflict``，
        创作合同冲突抛出 ``StoreAuthoringConflict``，其余身份、模板、图或元数据
        冲突抛出 ``StoreConflict``；异常由调用事务统一回滚，不留下部分写入。
        """

        workflow = self.get_workflow(workflow_uuid, conn=conn)
        if workflow["revision"] != expected_revision:
            raise StoreRevisionConflict(
                f"workflow revision {workflow['revision']} does not match "
                f"expected {expected_revision}"
            )
        node_by_uuid = {node.uuid: node for node in nodes}
        edge_by_uuid = {edge.uuid: edge for edge in edges}
        if len(node_by_uuid) != len(nodes):
            raise StoreConflict("duplicate workflow node UUID")
        if len(edge_by_uuid) != len(edges):
            raise StoreConflict("duplicate workflow edge UUID")
        for edge in edges:
            if (
                edge.source_node_uuid not in node_by_uuid
                or edge.target_node_uuid not in node_by_uuid
            ):
                raise StoreConflict(
                    f"edge {edge.uuid} references a node outside the submitted graph"
                )
        if inventory_requirements is not None:
            requirement_keys: set[str] = set()
            requirement_uuids: set[str] = set()
            for requirement in inventory_requirements:
                identity = requirement.uuid or ""
                key = requirement.requirement_key or identity
                if requirement.consume_node_uuid not in node_by_uuid:
                    raise StoreConflict(
                        "inventory requirement references a node outside the graph"
                    )
                if not key:
                    # 无身份的新需求在当前事务生成一次，返回后由前端
                    # 带回同一 UUID / requirement_key。
                    identity = str(uuid4())
                    key = identity
                elif not identity:
                    identity = str(uuid4())
                if identity in requirement_uuids or key in requirement_keys:
                    raise StoreConflict("duplicate workflow inventory requirement")
                requirement_uuids.add(identity)
                requirement_keys.add(key)
        template_uuids = sorted(
            {
                node.workflow_node_template_uuid
                for node in nodes
                if node.workflow_node_template_uuid is not None
            }
        )
        templates: Dict[str, Dict[str, Any]] = {}
        handles: Dict[str, Dict[str, Any]] = {}
        catalog_snapshot = (
            self._template_snapshot_provider.snapshot()
            if template_uuids and self._template_snapshot_provider is not None
            else None
        )
        if template_uuids:
            if catalog_snapshot is not None:
                catalog_nodes, catalog_handles = self._catalog_entities(
                    template_uuids,
                    snapshot=catalog_snapshot,
                )
                templates = {item["uuid"]: item for item in catalog_nodes}
                handles = {item["uuid"]: item for item in catalog_handles}
            else:
                marks = ",".join("?" for _ in template_uuids)
                template_rows = conn.execute(
                    f"""
                    SELECT * FROM workflow_node_template
                    WHERE uuid IN ({marks}) AND deleted_at IS NULL
                    """,
                    template_uuids,
                ).fetchall()
                templates = {
                    row["uuid"]: self._node_template_row(row) for row in template_rows
                }
                handle_rows = conn.execute(
                    f"""
                    SELECT * FROM workflow_handle_template
                    WHERE workflow_node_template_uuid IN ({marks})
                      AND deleted_at IS NULL
                    """,
                    template_uuids,
                ).fetchall()
                handles = {
                    row["uuid"]: self._handle_template_row(row) for row in handle_rows
                }
        effective_params = {
            node.uuid: self._graph_node_param(
                conn,
                node,
                catalog_snapshot=catalog_snapshot,
            )
            for node in nodes
        }
        effective_node_meta_data: Dict[str, Dict[str, Any]] = {}
        for node in nodes:
            existing_node = conn.execute(
                "SELECT meta_data FROM workflow_node WHERE uuid = ?",
                (node.uuid,),
            ).fetchone()
            effective_node_meta_data[node.uuid] = self._protected_metadata(
                node.meta_data,
                (existing_node["meta_data"] if existing_node is not None else None),
                enabled=protect_reserved_metadata,
            )
        try:
            validate_graph(
                nodes=nodes,
                edges=edges,
                templates=templates,
                handles=handles,
                effective_params=effective_params,
                workflow_meta_data=(
                    semantic_workflow_meta_data
                    if semantic_workflow_meta_data is not None
                    else workflow["meta_data"]
                ),
                node_meta_data=effective_node_meta_data,
                validate_workflow_io_contract=validate_workflow_io_contract,
            )
        except MissingTemplateError as exc:
            raise StoreNotFound(str(exc)) from exc
        except CodedGraphValidationError as exc:
            raise StoreAuthoringConflict(exc.code, str(exc)) from exc
        except GraphValidationError as exc:
            raise StoreAuthoringConflict("invalid_input", str(exc)) from exc
        now = utc_now()
        for node in nodes:
            self._upsert_node(
                conn,
                workflow_uuid,
                node,
                now,
                protect_reserved_metadata=protect_reserved_metadata,
                effective_param=effective_params[node.uuid],
                catalog_snapshot=catalog_snapshot,
            )
        for edge in edges:
            self._upsert_edge(
                conn,
                workflow_uuid,
                edge,
                now,
                protect_reserved_metadata=protect_reserved_metadata,
            )
        if inventory_requirements is not None:
            self._reconcile_inventory_requirements(
                conn,
                workflow_uuid=workflow_uuid,
                requirements=inventory_requirements,
                now=now,
            )
        self._soft_delete_omitted(
            conn,
            table="workflow_edge",
            workflow_uuid=workflow_uuid,
            retained=edge_by_uuid,
            now=now,
        )
        self._soft_delete_omitted(
            conn,
            table="workflow_node",
            workflow_uuid=workflow_uuid,
            retained=node_by_uuid,
            now=now,
        )
        next_revision = expected_revision + 1 if advance_revision else expected_revision
        conn.execute(
            "UPDATE workflow SET revision = ?, update_time = ? "
            "WHERE uuid = ? AND deleted_at IS NULL",
            (next_revision, now, workflow_uuid),
        )
        return next_revision

    def _reconcile_inventory_requirements(
        self,
        conn: sqlite3.Connection,
        *,
        workflow_uuid: str,
        requirements: List[WorkflowInventoryRequirementWrite],
        now: str,
    ) -> None:
        """在完整图 CAS 事务内替换逻辑数量库存需求。"""

        retained: list[str] = []
        seen_keys: set[str] = set()
        for sort_order, requirement in enumerate(requirements):
            identity = requirement.uuid or str(uuid4())
            requirement_key = requirement.requirement_key or identity
            if requirement_key in seen_keys:
                raise StoreConflict("duplicate workflow inventory requirement key")
            seen_keys.add(requirement_key)
            existing = conn.execute(
                "SELECT workflow_uuid,create_time FROM workflow_inventory_requirement "
                "WHERE uuid=?",
                (identity,),
            ).fetchone()
            if existing is not None and existing["workflow_uuid"] != workflow_uuid:
                raise StoreAuthoringConflict("candidate_identity_conflict")
            values = (
                now,
                None,
                requirement.description,
                _json(requirement.meta_data),
                workflow_uuid,
                requirement.consume_node_uuid,
                requirement_key,
                requirement.target_type,
                requirement.reagent_info_uuid,
                requirement.required_quantity,
                requirement.quantity_unit,
                int(requirement.allow_split),
                sort_order,
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO workflow_inventory_requirement(
                        uuid,create_time,update_time,deleted_at,description,meta_data,
                        workflow_uuid,consume_node_uuid,requirement_key,target_type,
                        reagent_info_uuid,required_quantity,quantity_unit,allow_split,
                        sort_order
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (identity, now, *values),
                )
            else:
                conn.execute(
                    """
                    UPDATE workflow_inventory_requirement
                    SET update_time=?,deleted_at=?,description=?,meta_data=?,
                        workflow_uuid=?,consume_node_uuid=?,requirement_key=?,
                        target_type=?,reagent_info_uuid=?,required_quantity=?,
                        quantity_unit=?,allow_split=?,sort_order=?
                    WHERE uuid=?
                    """,
                    (*values, identity),
                )
            retained.append(identity)
        self._soft_delete_omitted(
            conn,
            table="workflow_inventory_requirement",
            workflow_uuid=workflow_uuid,
            retained=retained,
            now=now,
        )

    def _upsert_node(
        self,
        conn: sqlite3.Connection,
        workflow_uuid: str,
        node: WorkflowNodeWrite,
        now: str,
        *,
        protect_reserved_metadata: bool,
        effective_param: Dict[str, Any],
        catalog_snapshot: AuthoringCatalogSnapshot | None = None,
    ) -> None:
        """按同一冻结模板代际写入一个工作流节点。"""

        existing = conn.execute(
            "SELECT workflow_uuid, create_time, meta_data "
            "FROM workflow_node WHERE uuid = ?",
            (node.uuid,),
        ).fetchone()
        if existing is not None and existing["workflow_uuid"] != workflow_uuid:
            raise StoreAuthoringConflict("candidate_identity_conflict")
        meta_data = self._protected_metadata(
            node.meta_data,
            existing["meta_data"] if existing is not None else None,
            enabled=protect_reserved_metadata,
        )
        template_reference = node.workflow_node_template_uuid
        if (
            template_reference is not None
            and self._template_snapshot_provider is not None
        ):
            try:
                snapshot = (
                    catalog_snapshot or self._template_snapshot_provider.snapshot()
                )
                template_reference = snapshot.template_key_for_uuid(template_reference)
            except AuthoringCatalogError as error:
                persisted = self._published_template_entities_for_reference(
                    template_reference
                )
                if persisted is None:
                    raise StoreNotFound(
                        f"workflow node template {template_reference} not found"
                    ) from error
                # 早期版本曾把组合模板 UUID 转成名称键后保存。恢复时立即规范化
                # 为发布合同固定 UUID，避免下一次目录重建只能看到设备基础目录。
                template_reference = persisted[0]["uuid"]
        values = (
            node.description,
            _json(meta_data),
            workflow_uuid,
            template_reference,
            node.parent_uuid,
            node.material_uuid,
            node.name,
            node.status,
            node.type,
            node.icon,
            _json(node.pose),
            _json(effective_param),
            _json(node.manual_confirmation),
            node.footer,
            node.action_name,
            node.action_type,
            _json(node.execution_policy),
            int(node.disabled),
            int(node.minimized),
            node.script,
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO workflow_node(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, workflow_node_template_uuid,
                    parent_uuid, material_uuid, name, status, type, icon, pose,
                    param, manual_confirmation, footer, action_name, action_type,
                    execution_policy,
                    disabled, minimized, script
                ) VALUES (
                    ?, ?, ?, NULL,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (node.uuid, now, now, *values),
            )
            return
        conn.execute(
            """
            UPDATE workflow_node
            SET update_time = ?, deleted_at = NULL, description = ?,
                meta_data = ?, workflow_uuid = ?,
                workflow_node_template_uuid = ?, parent_uuid = ?,
                material_uuid = ?, name = ?, status = ?, type = ?, icon = ?,
                pose = ?, param = ?, manual_confirmation = ?, footer = ?,
                action_name = ?, action_type = ?, execution_policy = ?, disabled = ?,
                minimized = ?, script = ?
            WHERE uuid = ?
            """,
            (now, *values, node.uuid),
        )

    def _graph_node_param(
        self,
        conn: sqlite3.Connection,
        node: WorkflowNodeWrite,
        *,
        catalog_snapshot: AuthoringCatalogSnapshot | None = None,
    ) -> Dict[str, Any]:
        """解析节点显式参数或所引用模板的默认参数。

        参数：``conn`` 仅供遗留 SQLite 模板适配器读取；``node`` 是候选节点；
        ``catalog_snapshot`` 是本事务已经冻结的内存目录代际。返回：普通参数字典。
        """

        if node.param is not None:
            return node.param
        if node.workflow_node_template_uuid is None:
            return {}
        if self._template_snapshot_provider is not None:
            snapshot = catalog_snapshot or self._template_snapshot_provider.snapshot()
            try:
                template = snapshot.require_template(
                    node.workflow_node_template_uuid
                ).template
            except AuthoringCatalogError:
                persisted = self._published_template_entities_for_reference(
                    node.workflow_node_template_uuid
                )
                if persisted is None:
                    return {}
                template = persisted[0]
            for field in ("goal_default", "goal"):
                fallback = template.get(field)
                if isinstance(fallback, Mapping) and fallback:
                    return dict(fallback)
            return {}
        template = conn.execute(
            """
            SELECT goal_default, goal
            FROM workflow_node_template
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (node.workflow_node_template_uuid,),
        ).fetchone()
        if template is None:
            return {}
        for field in ("goal_default", "goal"):
            fallback = _load(template[field], {})
            if isinstance(fallback, dict) and fallback:
                return fallback
        return {}

    def _upsert_edge(
        self,
        conn: sqlite3.Connection,
        workflow_uuid: str,
        edge: WorkflowEdgeWrite,
        now: str,
        *,
        protect_reserved_metadata: bool,
    ) -> None:
        existing = conn.execute(
            "SELECT workflow_uuid, meta_data FROM workflow_edge WHERE uuid = ?",
            (edge.uuid,),
        ).fetchone()
        if existing is not None and existing["workflow_uuid"] != workflow_uuid:
            raise StoreAuthoringConflict("candidate_identity_conflict")
        meta_data = self._protected_metadata(
            edge.meta_data,
            existing["meta_data"] if existing is not None else None,
            enabled=protect_reserved_metadata,
        )
        values = (
            edge.description,
            _json(meta_data),
            workflow_uuid,
            edge.source_node_uuid,
            edge.target_node_uuid,
            edge.source_handle_uuid,
            edge.target_handle_uuid,
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO workflow_edge(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, source_node_uuid,
                    target_node_uuid, source_handle_uuid, target_handle_uuid
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (edge.uuid, now, now, *values),
            )
            return
        conn.execute(
            """
            UPDATE workflow_edge
            SET update_time = ?, deleted_at = NULL, description = ?,
                meta_data = ?, workflow_uuid = ?, source_node_uuid = ?,
                target_node_uuid = ?, source_handle_uuid = ?,
                target_handle_uuid = ?
            WHERE uuid = ?
            """,
            (now, *values, edge.uuid),
        )

    @staticmethod
    def _protected_metadata(
        submitted: Dict[str, Any],
        existing_json: Optional[str],
        *,
        enabled: bool,
    ) -> Dict[str, Any]:
        """合并公开节点元数据，并保留服务端维护的源码顺序。

        参数：``submitted`` 是调用方提交的节点元数据，``existing_json`` 是同一
        节点原有的 JSON；``enabled`` 为真表示公共 Graph 接口。返回：公开字段与
        已有系统元数据合并后的对象。异常：非法 JSON 由上层统一转换。公共调用只
        接受非负整数 ``authoring_source_order`` 作为新节点的创建顺序，并允许
        输入绑定、循环 carry、控制区域和组合调用边界等创作语义穿过保护边界；
        执行器绑定仍由服务端保留，避免客户端伪造目录事实。
        """

        result = dict(submitted)
        if not enabled:
            return result
        submitted_unilab = result.pop("unilab", None)
        existing = _load(existing_json, {}) if existing_json is not None else {}
        existing_unilab = (
            existing.get("unilab") if isinstance(existing, dict) else None
        )
        if isinstance(existing_unilab, Mapping):
            # 节点的执行器、源码身份和组合展开事实由服务端维护；工作流
            # 输入绑定是前端编辑的业务语义，必须允许在保存时更新。只合并
            # 这一项，避免公共 Graph PUT 伪造其他保留字段。
            protected_unilab = deepcopy(dict(existing_unilab))
            if isinstance(submitted_unilab, Mapping):
                input_bindings = submitted_unilab.get("input_bindings")
                if input_bindings is not None:
                    protected_unilab["input_bindings"] = deepcopy(input_bindings)
            result["unilab"] = protected_unilab
            return result
        # 完整 Graph PUT / JSON 导入会一次性提交新控制节点。允许输入绑定、
        # 循环 carry、条件/循环区域标记和组合调用边界等创作语义穿过保护边界，
        # 否则导入后无法生成规范 Python；执行器绑定仍不可由客户端写入。
        if isinstance(submitted_unilab, Mapping):
            public_unilab = _public_create_unilab(submitted_unilab)
            if public_unilab:
                result["unilab"] = public_unilab
        return result

    @staticmethod
    def _soft_delete_omitted(
        conn: sqlite3.Connection,
        *,
        table: str,
        workflow_uuid: str,
        retained: Iterable[str],
        now: str,
    ) -> None:
        retained_values = list(retained)
        if retained_values:
            marks = ",".join("?" for _ in retained_values)
            conn.execute(
                f"""
                UPDATE {table}
                SET deleted_at = ?, update_time = ?
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                  AND uuid NOT IN ({marks})
                """,
                (now, now, workflow_uuid, *retained_values),
            )
        else:
            conn.execute(
                f"""
                UPDATE {table}
                SET deleted_at = ?, update_time = ?
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                """,
                (now, now, workflow_uuid),
            )

    # Task 与 Job --------------------------------------------------------

    @staticmethod
    def _execution_admission_blocker(
        connection: sqlite3.Connection,
        *,
        requested_run_mode: str,
        exclude_task_uuid: str | None = None,
    ) -> sqlite3.Row | None:
        """在一个已有事务中读取执行准入的唯一阻塞者。

        ``step`` 任务需要 Runtime 当前没有任何活动任务，也不能越过仍在清理的
        终态任务；普通任务在调试任务的单步状态或切换状态期间也必须等待。调用
        方持有同一个 SQLite 写事务时，查询与后续 INSERT/UPDATE 具备原子性，
        避免两个调试请求同时通过预检查。
        """

        active_statuses = "('pending', 'running', 'canceling')"
        status_filter = f"status IN {active_statuses}"
        mode_filter = ""
        if requested_run_mode == "step":
            status_filter = (
                f"({status_filter} OR cleanup_status NOT IN ('none', 'settled'))"
            )
        else:
            mode_filter = "AND execution_mode IN ('switching_to_step', 'step')"
        exclude_filter = ""
        parameters: tuple[str, ...] = ()
        if exclude_task_uuid:
            exclude_filter = "AND uuid <> ?"
            parameters = (exclude_task_uuid,)
        return connection.execute(
            f"""
            SELECT uuid, status, cleanup_status, workflow_uuid,
                   execution_kind, run_mode, execution_mode
            FROM workflow_task
            WHERE deleted_at IS NULL
              AND {status_filter}
              {mode_filter}
              {exclude_filter}
            ORDER BY create_time, uuid
            LIMIT 1
            """,
            parameters,
        ).fetchone()

    @staticmethod
    def execution_admission_blocker(
        connection: sqlite3.Connection,
        *,
        requested_run_mode: str,
        exclude_task_uuid: str | None = None,
    ) -> sqlite3.Row | None:
        """在事务内读取执行准入阻塞者，供其他工作流存储适配器复用。"""

        return WorkflowStore._execution_admission_blocker(
            connection,
            requested_run_mode=requested_run_mode,
            exclude_task_uuid=exclude_task_uuid,
        )

    def find_execution_admission_blocker(
        self,
        *,
        requested_run_mode: str,
        exclude_task_uuid: str | None = None,
    ) -> dict[str, Any] | None:
        """读取当前普通/单步执行请求的活动任务阻塞者。

        该方法只用于运行预检查；正式创建或切换仍必须在写事务内重新调用
        ``execution_admission_blocker``。
        """

        with self.read() as connection:
            row = self._execution_admission_blocker(
                connection,
                requested_run_mode=requested_run_mode,
                exclude_task_uuid=exclude_task_uuid,
            )
            return dict(row) if row is not None else None

    def evidence_only(self) -> bool:
        """灾备状态每次从持久库读取，不能被旧服务对象缓存绕过。"""
        from unilabos.persistence.recovery_mode import evidence_only

        with self.read() as conn:
            return evidence_only(conn)

    def execution_held(self, task_uuid: str) -> bool:
        from unilabos.persistence.recovery_mode import evidence_only, held

        with self.read() as conn:
            return evidence_only(conn) or held(conn, "task", task_uuid)

    def invocation_held(self, invocation_uuid: str) -> bool:
        """物料事实补报也遵守原调用隔离，不能借新纪元改写旧边界。"""
        from unilabos.persistence.recovery_mode import evidence_only, held

        with self.read() as conn:
            return evidence_only(conn) or held(conn, "invocation", invocation_uuid)

    def create_task_with_jobs(
        self,
        *,
        workflow_uuid: str,
        task_uuid: str,
        run_mode: str,
        target_node_uuid: Optional[str],
        description: Optional[str],
        meta_data: Dict[str, Any],
        plan_builder: Callable[[Dict[str, Any]], PreparedTaskInput],
        inventory_allocation_builder: Optional[
            Callable[
                [sqlite3.Connection, Dict[str, Any], PreparedTaskInput],
                List[Dict[str, Any]],
            ]
        ] = None,
        applied_graph: Dict[str, Any] | None = None,
        backend_task_uuid: str | None = None,
        invocation_key: str | None = None,
        priority: WorkflowTaskPriority | str | float = WorkflowTaskPriority.NORMAL,
        request_fingerprint: str = "",
        revision_fingerprint: str | None = None,
        deadline: str | None = None,
        validate_scheduler_compilation: bool = False,
        center_invocation_uuid: str | None = None,
    ) -> Dict[str, Any]:
        """原子创建工作流任务（WorkflowTask）及首次节点作业。

        参数：工作流、任务、运行模式与目标标识创建意图；说明和元数据是公开
        请求事实；``backend_task_uuid`` 与 ``invocation_key`` 可标识上游同一次
        工站调用，``priority`` 是本次任务的 ``normal``/``high`` 字符串枚举（旧
        工站调用仍可暂存数值权重），``request_fingerprint`` 防止同一调用键重放不同载荷；
        ``applied_graph`` 是进程内定义目录一次读取的不可变应用图，
        省略时仅为兼容持久定义 Store 从当前事务读取；``plan_builder`` 必须从该
        同一应用图返回已解析输入、冻结快照、执行计划（ExecutionPlan）及作业；可选
        ``inventory_allocation_builder`` 在首次写入前用同一工作流事务锁校验
        数量型库存绑定并返回既有分配表行；``center_invocation_uuid`` 要求当前
        事务同时验证已领取中心意图、绑定本地任务并持久化 accepted。
        返回：提交后的任务投影。
        异常：图不存在、计划或输入无效及数据库失败均回滚任务和全部作业写入。
        """

        stored_priority = _stored_task_priority(priority)
        now = utc_now()
        with self.transaction() as conn:
            from unilabos.persistence.recovery_mode import require_execution

            require_execution(conn, task_uuid=task_uuid, invocation_uuid=center_invocation_uuid,
                              backend_task_uuid=backend_task_uuid, invocation_key=invocation_key)
            if backend_task_uuid is not None or invocation_key is not None:
                if not backend_task_uuid or not invocation_key or not request_fingerprint:
                    raise StoreConflict("Backend 工站调用身份或请求指纹不完整")
                existing = conn.execute(
                    """
                    SELECT * FROM workflow_task
                    WHERE backend_task_uuid = ? AND invocation_key = ?
                    """,
                    (backend_task_uuid, invocation_key),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"] or "") != request_fingerprint:
                        raise StoreConflict("同一工站调用键对应的请求内容已变化")
                    if center_invocation_uuid is not None:
                        from unilabos.workflow.center_boundary import (
                            bind_center_invocation,
                            record_center_task_outcome,
                        )

                        bind_center_invocation(
                            conn, invocation_uuid=center_invocation_uuid,
                            task_uuid=str(existing["uuid"]),
                        )
                        record_center_task_outcome(conn, task_uuid=str(existing["uuid"]))
                    result = self._task_row(existing)
                    result["_station_submission_created"] = False
                    return result
            blocker = self._execution_admission_blocker(
                conn,
                requested_run_mode=run_mode,
            )
            if blocker is not None:
                raise StoreConflict(
                    "debug_task_conflict:"
                    f"{blocker['uuid']}:{blocker['status']}"
                )
            if applied_graph is None:
                if not self._persist_workflow_definitions:
                    raise StoreConflict("运行事实库创建任务时缺少工作流图快照")
                graph = self.get_graph(workflow_uuid, conn=conn)
            else:
                graph = applied_graph
            prepared = plan_builder(graph)
            from unilabos.workflow.resource_lock_plan import normalize_execution_resource_plan

            plan = normalize_execution_resource_plan(prepared.execution_plan)
            jobs = prepared.jobs
            inventory_allocations = (
                inventory_allocation_builder(conn, graph, prepared)
                if inventory_allocation_builder is not None
                else []
            )
            effective_run_mode = str(plan["run_mode"])
            effective_target = plan.get("target_node_uuid")
            control_status = "paused" if effective_run_mode == "step" else "active"
            dispatch_gate = "paused" if effective_run_mode == "step" else "active"
            conn.execute(
                """
                INSERT INTO workflow_task(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, status, workflow_snapshot,
                    execution_plan, run_mode, execution_mode, dispatch_gate,
                    target_node_uuid,
                    control_status,
                    cleanup_status, trace_context, input, output, error_info,
                    backend_task_uuid, invocation_key, priority,
                    request_fingerprint, revision_fingerprint, timeout_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?,
                          'none', '{}', ?, '{}', '[]', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_uuid,
                    now,
                    now,
                    description,
                    _json(meta_data),
                    workflow_uuid,
                    _json(prepared.workflow_snapshot),
                    _json(plan),
                    effective_run_mode,
                    "step" if effective_run_mode == "step" else "normal",
                    dispatch_gate,
                    effective_target,
                    control_status,
                    _json(prepared.resolved_input),
                    backend_task_uuid,
                    invocation_key,
                    stored_priority,
                    request_fingerprint,
                    revision_fingerprint,
                    deadline,
                ),
            )
            from unilabos.workflow.station_status_projection import (
                append_job_state_event,
                append_task_state_event,
            )

            append_task_state_event(
                conn,
                task_uuid=task_uuid,
                status="pending",
                details={
                    "backend_task_uuid": backend_task_uuid,
                    "invocation_key": invocation_key,
                    "workflow_uuid": workflow_uuid,
                    "priority": stored_priority,
                },
            )
            self._append_runtime_event(
                conn,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status=None,
                to_status="pending",
                now=now,
            )
            for job in jobs:
                initial_status = str(job.get("status") or "pending")
                if initial_status not in {"pending", "succeeded"}:
                    raise StoreConflict("首次作业状态只能是 pending 或 succeeded")
                initial_return_info = job.get("return_info") or {}
                initial_finished_at = now if initial_status == "succeeded" else None
                conn.execute(
                    """
                    INSERT INTO workflow_node_job(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, workflow_task_uuid, workflow_node_uuid,
                        material_uuid, feedback_sequence, topological_index,
                        executor_kind, execution_policy,
                        execution_timeout_seconds, status, attempt, param,
                        feedback_data, return_info, control_data, error_info,
                        finished_at
                    ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, 0, ?, ?, ?,
                              ?, ?, 1, ?, '{}', ?, '{}', '[]', ?)
                    """,
                    (
                        job["uuid"],
                        now,
                        now,
                        task_uuid,
                        job["workflow_node_uuid"],
                        job.get("material_uuid"),
                        job["topological_index"],
                        job["executor_kind"],
                        _json(job.get("execution_policy") or {}),
                        int(job.get("execution_timeout_seconds") or 0),
                        initial_status,
                        _json(job.get("param") or {}),
                        _json(initial_return_info),
                        initial_finished_at,
                    ),
                )
                self._append_runtime_event(
                    conn,
                    task_uuid=task_uuid,
                    job_uuid=job["uuid"],
                    kind="job_transition",
                    from_status=None,
                    to_status=initial_status,
                    now=now,
                )
                job_row = conn.execute(
                    "SELECT * FROM workflow_node_job WHERE uuid = ?",
                    (job["uuid"],),
                ).fetchone()
                assert job_row is not None
                append_job_state_event(
                    conn,
                    job_row=job_row,
                    status=initial_status,
                    details={
                        "param": job.get("param") or {},
                        "return_info": initial_return_info,
                    },
                )
            if center_invocation_uuid is not None:
                from unilabos.workflow.center_boundary import bind_center_invocation

                # 绑定、accepted 与首次 Task/Jobs 必须一并提交，后续终态投影复用此绑定。
                bind_center_invocation(
                    conn, invocation_uuid=center_invocation_uuid, task_uuid=task_uuid,
                )
            for allocation in inventory_allocations:
                conn.execute(
                    """
                    INSERT INTO workflow_inventory_allocation(
                        uuid,workflow_task_uuid,workflow_node_job_uuid,
                        requirement_key,inventory_type,inventory_uuid,
                        material_uuid,reserved_quantity,quantity_unit,status,
                        revision,reserved_at,consumed_at,released_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,'reserved',1,?,NULL,NULL)
                    """,
                    (
                        allocation["uuid"],
                        task_uuid,
                        allocation["workflow_node_job_uuid"],
                        allocation["requirement_key"],
                        allocation["inventory_type"],
                        allocation["inventory_uuid"],
                        allocation["material_uuid"],
                        allocation["reserved_quantity"],
                        allocation["quantity_unit"],
                        now,
                    ),
                )
            if inventory_allocations:
                conn.execute(
                    """
                    INSERT INTO workflow_inventory_saga(
                        workflow_task_uuid,status,operation_key,payload,last_error,
                        attempt,update_time
                    ) VALUES (?, 'reserved', ?, ?, NULL, 1, ?)
                    """,
                    (
                        task_uuid,
                        f"reserve:{task_uuid}",
                        _json(
                            {
                                "allocation_uuids": [
                                    allocation["uuid"] for allocation in inventory_allocations
                                ]
                            }
                        ),
                        now,
                    ),
                )
            from unilabos.workflow.workflow_boundary import (
                project_ready_workflow_output,
            )

            boundary = project_ready_workflow_output(
                conn,
                task_uuid=task_uuid,
                now=now,
                complete_task=True,
            )
            task_completed = boundary.task_completed
            if not task_completed:
                job_counts = conn.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status != 'succeeded' THEN 1 ELSE 0 END)
                               AS unfinished
                    FROM workflow_node_job
                    WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                    """,
                    (task_uuid,),
                ).fetchone()
                assert job_counts is not None
                task_completed = bool(
                    int(job_counts["total"] or 0) > 0 and int(job_counts["unfinished"] or 0) == 0
                )
                if task_completed:
                    conn.execute(
                        """
                        UPDATE workflow_task
                        SET status = 'succeeded', update_time = ?, finished_at = ?
                        WHERE uuid = ? AND status = 'pending'
                        """,
                        (now, now, task_uuid),
                    )
            if boundary.output_changed and boundary.output_job_uuid is not None:
                self._append_runtime_event(
                    conn,
                    task_uuid=task_uuid,
                    job_uuid=boundary.output_job_uuid,
                    kind="job_transition",
                    from_status="pending",
                    to_status="succeeded",
                    now=now,
                )
                boundary_row = conn.execute(
                    "SELECT * FROM workflow_node_job WHERE uuid = ?",
                    (boundary.output_job_uuid,),
                ).fetchone()
                assert boundary_row is not None
                append_job_state_event(
                    conn,
                    job_row=boundary_row,
                    status="succeeded",
                    details={"return_info": boundary.result or {}},
                )
            if task_completed:
                self._append_runtime_event(
                    conn,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status="pending",
                    to_status="succeeded",
                    now=now,
                )
                append_task_state_event(
                    conn,
                    task_uuid=task_uuid,
                    status="succeeded",
                    details={"finished_at": now},
                )
            self._append_event(
                conn,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )
            if validate_scheduler_compilation and not task_completed:
                # 在创建事务内检查冻结计划。库存实例绑定在提交调度时完成。
                from unilabos.workflow.workflow_spec_compiler import (
                    WorkflowSpecCompilationError,
                    WorkflowSpecCompiler,
                )

                try:
                    task_snapshot = self.get_task(task_uuid)
                    jobs = self.list_jobs(task_uuid)
                    compiler = WorkflowSpecCompiler()
                    if task_snapshot["execution_plan"].get("inventory_resource_binding") == "pending":
                        compiler.validate_inventory_admission(task_snapshot, jobs)
                    else:
                        compiler.compile(task_snapshot, jobs)
                except WorkflowSpecCompilationError as error:
                    raise StoreConflict(f"工作流任务不能提交调度：{error}") from error
        result = self.get_task(task_uuid)
        if backend_task_uuid is not None:
            result["_station_submission_created"] = True
        return result

    def get_task(self, task_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT task.*, model.definition_version FROM workflow_task AS task "
                "JOIN workflow_task_read_model AS model ON model.task_uuid=task.uuid "
                "WHERE task.uuid = ? AND task.deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow task {task_uuid} not found")
        return self._task_row(row)

    def center_attention_state(self) -> tuple[Any, ...]:
        """读取已绑定中心任务的原生事项小状态，供提交后的通知去重。"""
        from unilabos.workflow.center_attention import current_state
        with self._lock:
            return current_state(self._conn)

    def center_attention_snapshot(self) -> Dict[str, Any]:
        """读取当前仍需中心展示或人工处理的事项快照。"""

        from unilabos.workflow.center_attention import current_snapshot

        with self._lock:
            return current_snapshot(self._conn)

    @staticmethod
    def _task_delete_eligibility(
        connection: sqlite3.Connection,
        task_row: sqlite3.Row,
    ) -> Dict[str, Any]:
        """在调用方事务内计算任务软删除资格。

        参数：``connection`` 是已经持有 ``BEGIN IMMEDIATE`` 的工作流库连接，
        ``task_row`` 是包含已删除记录在内的任务行。返回：面向控制台的稳定资格
        投影；所有阻塞原因都来自同一事务中的持久事实。异常：查询错误原样传播。
        状态不变量：本函数只读，不改变任何任务、Job 或资源记录。
        """

        task_uuid = str(task_row["uuid"])
        deleted_at = task_row["deleted_at"]
        status = str(task_row["status"] or "")
        cleanup_status = str(task_row["cleanup_status"] or "none")
        blockers: list[str] = []
        if deleted_at is not None:
            return {
                "can_delete": False,
                "already_deleted": True,
                "delete_blockers": [],
                "status": status,
                "cleanup_status": cleanup_status,
                "deleted_at": deleted_at,
            }
        if status not in _TASK_DELETE_TERMINAL_STATUSES:
            blockers.append(f"任务状态为 {status or 'unknown'}，尚未进入终态")
        if cleanup_status not in {"none", "settled"}:
            blockers.append(f"任务清理状态为 {cleanup_status}，资源尚未完全收敛")

        jobs = connection.execute(
            """
            SELECT status, uncertainty_reason
            FROM workflow_node_job
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL
            """,
            (task_uuid,),
        ).fetchall()
        non_terminal_jobs = sum(
            str(row["status"] or "") not in _JOB_DELETE_TERMINAL_STATUSES
            for row in jobs
        )
        uncertain_jobs = sum(bool(str(row["uncertainty_reason"] or "").strip()) for row in jobs)
        if non_terminal_jobs:
            blockers.append(f"仍有 {non_terminal_jobs} 个 Job 未进入终态")
        if uncertain_jobs:
            blockers.append(f"仍有 {uncertain_jobs} 个 Job 存在结果不确定原因")

        def count(sql: str, values: tuple[Any, ...] = ()):  # local transaction helper
            try:
                return int(connection.execute(sql, values).fetchone()[0])
            except sqlite3.OperationalError as error:
                # 干预/人工确认表按需迁移；旧历史库尚未使用这些能力时，
                # 缺表等价于没有该类残留，不能让任务详情接口整体 500。
                message = str(error).lower()
                if (
                    "no such table" in message
                    or ("relation " in message and "does not exist" in message)
                ):
                    return 0
                raise

        active_claims = count(
            """
            SELECT COUNT(*) FROM execution_claim
            WHERE workflow_task_uuid = ? AND state != 'released'
            """,
            (task_uuid,),
        )
        if active_claims:
            blockers.append(f"仍有 {active_claims} 个执行 Claim 未释放")
        active_leases = count(
            """
            SELECT COUNT(*) FROM execution_lock_lease
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL AND state != 'released'
            """,
            (task_uuid,),
        )
        if active_leases:
            blockers.append(f"仍有 {active_leases} 个执行锁租约未释放")
        waiting_locks = count(
            """
            SELECT COUNT(*) FROM execution_lock_waiter
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL AND state = 'waiting'
            """,
            (task_uuid,),
        )
        if waiting_locks:
            blockers.append(f"仍有 {waiting_locks} 个资源锁请求在等待")
        active_tenancies = count(
            """
            SELECT COUNT(*) FROM task_device_tenancy
            WHERE workflow_task_uuid = ? AND state = 'active'
            """,
            (task_uuid,),
        )
        if active_tenancies:
            blockers.append(f"仍有 {active_tenancies} 个设备托管未释放")
        active_material_claims = count(
            """
            SELECT COUNT(*) FROM workflow_task_material_claim
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL AND status = 'active'
            """,
            (task_uuid,),
        )
        if active_material_claims:
            blockers.append(f"仍有 {active_material_claims} 个物料 Claim 未释放")
        pending_commands = count(
            """
            SELECT COUNT(*) FROM workflow_task_command
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL AND status = 'pending'
            """,
            (task_uuid,),
        )
        if pending_commands:
            blockers.append(f"仍有 {pending_commands} 个任务控制命令待处理")
        # 节点错误的已解决事实在 stage 上；保留 open/selected 的历史决定状态。
        # 设备干预、未知类型或阶段继续沿用原门禁，只排除明确已解决的节点错误。
        open_interventions = count(
            """
            SELECT COUNT(*) FROM workflow_intervention
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL
              AND status IN ('open', 'selected')
              AND NOT (COALESCE(decision_kind, '') = 'edge_node_error'
                       AND COALESCE(stage, '') = 'RESOLVED')
            """,
            (task_uuid,),
        )
        if open_interventions:
            blockers.append(f"仍有 {open_interventions} 个人工干预未关闭")
        # approved 是已提交的人工决定；动作是否结束由 Job、任务和资源门禁判断。
        pending_confirmations = count(
            """
            SELECT COUNT(*) FROM workflow_manual_confirmation
            WHERE workflow_task_uuid = ? AND status = 'pending'
            """,
            (task_uuid,),
        )
        if pending_confirmations:
            blockers.append(f"仍有 {pending_confirmations} 个人工确认未完成")
        reserved_inventory = count(
            """
            SELECT COUNT(*) FROM workflow_inventory_allocation
            WHERE workflow_task_uuid = ? AND status = 'reserved'
            """,
            (task_uuid,),
        )
        if reserved_inventory:
            blockers.append(f"仍有 {reserved_inventory} 个数量库存分配未释放")
        pending_inventory_saga = count(
            """
            SELECT COUNT(*) FROM workflow_inventory_saga
            WHERE workflow_task_uuid = ? AND status != 'settled'
            """,
            (task_uuid,),
        )
        if pending_inventory_saga:
            blockers.append("数量库存 Saga 尚未结算")
        return {
            "can_delete": not blockers,
            "already_deleted": False,
            "delete_blockers": blockers,
            "status": status,
            "cleanup_status": cleanup_status,
            "deleted_at": None,
        }

    def task_delete_eligibility(self, task_uuid: str) -> Dict[str, Any]:
        """读取任务软删除资格；普通详情只读使用，不返回已删除任务。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT uuid, deleted_at, status, cleanup_status FROM workflow_task WHERE uuid = ?",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            return self._task_delete_eligibility(self._conn, row)

    def delete_task(self, task_uuid: str) -> Dict[str, Any]:
        """事务内软删除一个已终态且全部资源已释放的任务。

        参数：``task_uuid`` 是稳定任务 UUID。返回：删除结果，重复请求返回
        ``already_deleted``。异常：不存在抛 ``StoreNotFound``；任务未满足安全
        门禁抛带阻塞原因的 ``StoreConflict``。状态不变量：只更新根任务的
        ``deleted_at``，保留 Job、运行事件、库存和设备历史，避免破坏审计外键。
        """

        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_task WHERE uuid = ?",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            eligibility = self._task_delete_eligibility(connection, row)
            if eligibility["already_deleted"]:
                return {
                    "status": "already_deleted",
                    "workflow_task_uuid": task_uuid,
                    "deleted_at": eligibility["deleted_at"],
                }
            if not eligibility["can_delete"]:
                raise StoreConflict(
                    "任务当前不能删除：" + "；".join(eligibility["delete_blockers"]),
                )
            self._append_runtime_event(
                connection,
                task_uuid=task_uuid,
                # 运行日志的 kind 是持久化闭集；具体删除动作放在事件数据中，
                # 保持旧版本读取器可重放，同时保留不可变删除审计。
                kind="task_transition",
                now=now,
                from_status=eligibility["status"],
                to_status=eligibility["status"],
                data={
                    "event": "task_deleted",
                    "status": eligibility["status"],
                    "cleanup_status": eligibility["cleanup_status"],
                    "delete_blockers": [],
                },
            )
            updated = connection.execute(
                """
                UPDATE workflow_task
                SET deleted_at = ?, update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (now, now, task_uuid),
            ).rowcount
            if updated != 1:
                # BEGIN IMMEDIATE 已经串行化本地调度器；这里仍保留 CAS 保护，
                # 避免未来迁移到共享连接后误报告已删除。
                raise StoreConflict("任务状态已变化，请刷新后重试")
            self._append_event(
                connection,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid, "event": "task_deleted"},
                now=now,
            )
            return {
                "status": "deleted",
                "workflow_task_uuid": task_uuid,
                "deleted_at": now,
            }

    def get_station_task_by_invocation(
        self,
        *,
        backend_task_uuid: str,
        invocation_key: str,
        request_fingerprint: str,
    ) -> Dict[str, Any] | None:
        """按上游调用身份幂等读取首次冻结的工站任务。

        参数：``backend_task_uuid`` 与 ``invocation_key`` 唯一标识一次工站调用；
        ``request_fingerprint`` 证明重放入口请求未变化。返回既有任务，首次调用返回
        ``None``。异常：同一调用身份对应不同请求时抛 ``StoreConflict``；只读过程
        不解析当前工作流修订，因此定义演进不会破坏已冻结调用的网络重放。

        这里**故意不过滤软删除**：调用身份一经使用即永久占用，任务被软删除只表示
        不再执行，不表示可以再创建一次。过滤软删除会让同一调用键再建出一个任务，
        即同一次调用被执行两次。重放返回的既有任务可能带 ``deleted_at``，由调用方
        按"已删除的原任务"呈现，而不是新建。
        """

        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_task
                WHERE backend_task_uuid = ? AND invocation_key = ?
                """,
                (backend_task_uuid, invocation_key),
            ).fetchone()
        if row is None:
            return None
        if str(row["request_fingerprint"] or "") != request_fingerprint:
            raise StoreConflict("同一工站调用键对应的请求内容已变化")
        return self._task_row(row)

    def center_cancellation_requested(self, task_uuid: str) -> bool:
        """派发前读取原调用的持久取消；不把通知当作已取消的执行事实。"""
        from unilabos.workflow.center_boundary import cancellation_requested

        with self._lock:
            return cancellation_requested(self._conn, task_uuid)

    def center_material_input_receipt(self, task_uuid: str) -> Dict[str, Any] | None:
        """派发每个真实 Job 时携带原输入占用证明，库存库丢失时禁止放行。"""
        import json

        from unilabos.persistence.inspection import table_exists
        from unilabos.workflow.center_boundary import material_input_task_uuid

        with self.transaction() as connection:
            if not table_exists(connection, "center_invocation_inbox"):
                return None
            row = connection.execute("SELECT invocation_uuid FROM center_invocation_inbox WHERE local_task_uuid=?",
                                     (task_uuid,)).fetchone()
            if row is None or material_input_task_uuid(connection, row["invocation_uuid"]) is None:
                return None
            proof = connection.execute("SELECT receipt_json FROM center_material_input WHERE invocation_uuid=?",
                                       (row["invocation_uuid"],)).fetchone()
            return json.loads(proof["receipt_json"])

    def recover_center_workflow_invocation(self, invocation_uuid: str) -> Dict[str, Any] | None:
        """从冻结中心收件箱恢复已存在的 Task 绑定与事件，绝不创建或调度执行。"""
        from unilabos.workflow.center_boundary import recover_center_invocation

        with self.transaction() as conn:
            task_uuid = recover_center_invocation(conn, invocation_uuid=invocation_uuid)
            if task_uuid is None:
                return None
            row = conn.execute("SELECT * FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()
            if row is None:
                raise StoreConflict("中心调用绑定的本地任务不存在")
            # 永久调用身份包含软删除 Task，不能因删除而再次创建执行。
            return self._task_row(row)

    def get_station_workflow_result(self, task_uuid: str) -> Dict[str, Any] | None:
        """读取实际输出边界事实，缺失与合法空对象结果必须区分。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM workflow_result WHERE workflow_task_uuid=?", (task_uuid,)
            ).fetchone()
        return _load(row[0], {}) if row is not None else None

    def record_center_outcome_evidence(
        self, *, invocation_uuid: str, task_uuid: str, owner_instance_uuid: str,
        task_payload_digest: str, evidence: Dict[str, Any],
    ) -> None:
        """受信任站内适配器补交持久结果引用；不会调度、重试或重建任何物理 Job。"""
        from unilabos.workflow.center_boundary import record_center_outcome_evidence

        with self.transaction() as connection:
            record_center_outcome_evidence(
                connection, invocation_uuid=invocation_uuid, task_uuid=task_uuid,
                owner_instance_uuid=owner_instance_uuid, task_payload_digest=task_payload_digest, evidence=evidence,
            )

    def center_material_attention_contexts(self) -> List[Dict[str, Any]]:
        """只读经过原领取/核实绑定校验的中心调用、执行状态及结果补报原因。"""
        from unilabos.persistence.inspection import table_exists
        from unilabos.workflow.center_boundary import _intent
        with self.transaction() as connection:
            if not table_exists(connection, "center_invocation_inbox"):
                return []
            rows = connection.execute("""SELECT i.*, t.status AS task_status,
                t.finished_at, t.create_time AS created_at, e.pending_reason
                FROM center_invocation_inbox i JOIN workflow_task t ON t.uuid=i.local_task_uuid
                LEFT JOIN center_outcome_evidence e ON e.invocation_uuid=i.invocation_uuid""").fetchall()
            result = []
            for row in rows:
                try:
                    inbox, package, runtime = _intent(connection, row["invocation_uuid"])
                except StoreConflict:
                    # 缺失或冲突的原调用由既有恢复保护处理，不能借补报重建身份。
                    continue
                if inbox["local_task_uuid"] != row["local_task_uuid"]:
                    continue
                result.append({**dict(row), "package": package,
                    "instance_uuid": runtime["instance_uuid"], "event_epoch_uuid": runtime["event_epoch_uuid"]})
            return result

    def center_material_settlement(self, invocation_uuid: str) -> Dict[str, Any] | None:
        """读取独立的取消结算责任；不改变 Task 终态或普通结果事件。"""
        from unilabos.app.center_client.material_settlement import settlement_state
        return settlement_state(self, invocation_uuid)

    def center_material_source_context(self, invocation_uuid: str) -> Dict[str, Any] | None:
        """只读原成功任务的冻结调用关联；交接取消不得由容器反推全局任务。"""
        from unilabos.persistence.inspection import table_exists
        from unilabos.workflow.center_boundary import _intent, inspect_existing_task, material_input_task_uuid

        with self.transaction() as connection:
            if not table_exists(connection, "center_invocation_inbox"):
                return None
            inbox = connection.execute("SELECT local_task_uuid FROM center_invocation_inbox WHERE invocation_uuid=?",
                                       (invocation_uuid,)).fetchone()
            if inbox is None or not inbox["local_task_uuid"]:
                return None
            if self.invocation_held(invocation_uuid) or self.execution_held(inbox["local_task_uuid"]):
                return None
            try:
                intent, package, runtime = _intent(connection, invocation_uuid)
                task = inspect_existing_task(connection, package, local_task_uuid=inbox["local_task_uuid"], allow_terminal=True)
                planned = material_input_task_uuid(connection, invocation_uuid, existing_terminal=True)
            except StoreConflict:
                return None
            if ((planned is not None and planned != task["uuid"]) or intent["acceptance_version"] != 1
                    or connection.execute("SELECT 1 FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=0", (invocation_uuid,)).fetchone() is None
                    or connection.execute("SELECT 1 FROM center_invocation_inbox WHERE local_task_uuid=? AND invocation_uuid<>?",
                                          (task["uuid"], invocation_uuid)).fetchone() is not None):
                return None
            if task["status"] != "succeeded" or task["cleanup_status"] not in {"none", "settled"}:
                return None
            return {"invocation_uuid": invocation_uuid, "local_task_uuid": task["uuid"], "package": package,
                    "instance_uuid": runtime["instance_uuid"], "event_epoch_uuid": runtime["event_epoch_uuid"]}

    def center_recorded_cancellation(self, invocation_uuid: str) -> Dict[str, Any] | None:
        """读取历史取消误报的独立核实记录；不改写普通结果及取消结算回执。"""
        from unilabos.app.center_client.recorded_cancellation import recorded_cancellation_state

        return recorded_cancellation_state(self, invocation_uuid)

    def list_center_material_exports(self, *, limit: int = 100, after_invocation_uuid: str = "") -> List[Dict[str, Any]]:
        """公开导出待补报的真实终态边界，不向库存适配器暴露工作流私有表或设备图。"""
        from unilabos.workflow.center_boundary import list_material_exports

        with self.transaction() as connection:
            return list_material_exports(connection, limit=limit, after_invocation_uuid=after_invocation_uuid)

    def get_center_outcome_evidence_status(self, invocation_uuid: str) -> Dict[str, Any] | None:
        """读取本地待补报诊断；不会将终态物理任务退回可执行状态。"""
        from unilabos.persistence.inspection import table_exists

        with self._lock:
            if not table_exists(self._conn, "center_outcome_evidence"):
                return None
            row = self._conn.execute("SELECT * FROM center_outcome_evidence WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
        return dict(row) if row is not None else None

    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        workflow_uuid: Optional[str] = None,
        execution_kind: str = "",
        status: str = "",
        cleanup_status: str = "",
    ) -> Dict[str, Any]:
        """按 Backend 查询合同分页读取工作流任务（WorkflowTask）。

        参数：``page/page_size`` 控制分页；``workflow_uuid`` 限定工作流定义；
        ``execution_kind`` 区分工作流运行与设备单动作运行（DeviceActionRun）；
        ``status/cleanup_status`` 分别限定业务状态和清理状态。返回分页任务投影。
        """

        from unilabos.workflow.task_read_model import task_filter_sql

        where, values = task_filter_sql(
            workflow_uuid=workflow_uuid, execution_kind=execution_kind,
            status=status, cleanup_status=cleanup_status,
        )
        offset = (page - 1) * page_size
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM workflow_task WHERE {where}",
                values,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_task WHERE {where}
                ORDER BY create_time DESC, uuid
                LIMIT ? OFFSET ?
                """,
                (*values, page_size, offset),
            ).fetchall()
        return {
            "items": [self._task_row(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def count_tasks(
        self, *, workflow_uuid: str | None = None, execution_kind: str = "",
        status: str = "", cleanup_status: str = "",
    ) -> int:
        """只计算符合条件的任务数，不查询任务内容或 Job。"""
        from unilabos.workflow.task_read_model import task_filter_sql

        where, values = task_filter_sql(
            workflow_uuid=workflow_uuid, execution_kind=execution_kind,
            status=status, cleanup_status=cleanup_status,
        )
        with self._lock:
            return int(self._conn.execute(
                f"SELECT COUNT(*) FROM workflow_task WHERE {where}", values,
            ).fetchone()[0])

    def list_task_summaries(
        self, *, page: int, page_size: int, workflow_uuid: str | None = None,
        execution_kind: str = "", status: str = "", cleanup_status: str = "",
    ) -> Dict[str, Any]:
        """分页读取表格摘要及聚合进度，不传输或解码冻结图。"""
        from unilabos.workflow.task_read_model import (
            task_filter_sql,
            task_job_counts,
            task_projection_sql,
        )

        where, values = task_filter_sql(
            workflow_uuid=workflow_uuid, execution_kind=execution_kind,
            status=status, cleanup_status=cleanup_status, prefix="task.",
        )
        with self._lock:
            total = self.count_tasks(
                workflow_uuid=workflow_uuid, execution_kind=execution_kind,
                status=status, cleanup_status=cleanup_status,
            )
            rows = self._conn.execute(
                task_projection_sql(state=False) + f" WHERE {where} "
                "ORDER BY task.create_time DESC, task.uuid LIMIT ? OFFSET ?",
                (*values, page_size, (page - 1) * page_size),
            ).fetchall()
            counts = task_job_counts(self._conn, [row["uuid"] for row in rows])
        return {
            "items": [self._task_state_row(row, counts.get(row["uuid"], {})) for row in rows],
            "total": total, "page": page, "page_size": page_size,
        }

    def get_task_state(self, task_uuid: str) -> Dict[str, Any]:
        """读取详情动态状态与定义版本，完整图由未裁剪的详情接口按需读取。"""
        from unilabos.workflow.task_read_model import task_job_counts, task_projection_sql

        with self._lock:
            row = self._conn.execute(
                task_projection_sql(state=True) + " WHERE task.uuid=? AND task.deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            counts = task_job_counts(self._conn, [task_uuid])
        return self._task_state_row(row, counts.get(task_uuid, {}))

    @classmethod
    def _task_state_row(cls, row: sqlite3.Row, counts: dict[str, int]) -> Dict[str, Any]:
        """复用任务动态字段合同，移除占位图并加入小型统计。"""
        result = cls._task_row(row)
        result.pop("workflow_snapshot", None)
        result.pop("execution_plan", None)
        total = counts.get("total", 0)
        completed = counts.get("completed", 0)
        result.update({
            "workflow_name": row["workflow_name"],
            "progress": {
                "total": total, "completed": completed,
                "percent": int(completed * 100 / total + 0.5) if total else 0,
            },
            "job_counts": {key: counts.get(key, 0) for key in ("succeeded", "failed", "canceled")},
        })
        return result

    def manual_review_attention_resolved_for_tasks(
        self, task_uuids: Iterable[str],
    ) -> Dict[str, bool]:
        """只读确认独立手动 Task 的剩余提醒是否仅来自已核对结果。"""
        identities = tuple(dict.fromkeys(str(value) for value in task_uuids if value))
        resolved = dict.fromkeys(identities, False)
        if not identities:
            return resolved
        placeholders = ", ".join("?" for _ in identities)
        required_tables = {
            "execution_claim", "execution_lock_lease", "task_device_tenancy",
            "workflow_task_material_claim", "workflow_task_command",
            "workflow_intervention", "workflow_manual_confirmation", "workflow_error_session",
        }
        with self._lock:
            existing_tables = table_names(self._conn)
            if not required_tables <= existing_tables:
                return resolved
            rows = self._conn.execute(
                f"""SELECT j.*, t.attention_reason AS task_attention_reason
                FROM workflow_task t JOIN workflow_node_job j ON j.workflow_task_uuid=t.uuid
                WHERE t.uuid IN ({placeholders}) AND t.deleted_at IS NULL AND j.deleted_at IS NULL
                AND t.status IN ('succeeded','failed','canceled','timeout')
                AND t.control_status IN ('active','waiting_reconciliation')
                AND t.cleanup_status IN ('none','settled','requires_attention')
                AND j.execution_source='error_manual' AND j.current_attempt=1
                AND j.status IN ('succeeded','failed','canceled','timeout')
                AND (SELECT COUNT(*) FROM workflow_node_job other
                     WHERE other.workflow_task_uuid=t.uuid AND other.deleted_at IS NULL)=1
                AND EXISTS (SELECT 1 FROM workflow_error_session s
                            WHERE s.session_id=json_extract(j.meta_data,'$.session_id'))
                AND NOT EXISTS (SELECT 1 FROM execution_claim c
                                WHERE c.workflow_task_uuid=t.uuid AND c.state!='released')
                AND NOT EXISTS (SELECT 1 FROM execution_lock_lease l
                                WHERE l.workflow_task_uuid=t.uuid AND l.deleted_at IS NULL AND l.state!='released')
                AND NOT EXISTS (SELECT 1 FROM task_device_tenancy d
                                WHERE d.workflow_task_uuid=t.uuid AND d.state!='released')
                AND NOT EXISTS (SELECT 1 FROM workflow_task_material_claim m
                                WHERE m.workflow_task_uuid=t.uuid AND m.deleted_at IS NULL AND m.status='active')
                AND NOT EXISTS (SELECT 1 FROM workflow_task_command c
                                WHERE c.workflow_task_uuid=t.uuid AND c.status='pending')
                AND NOT EXISTS (SELECT 1 FROM workflow_intervention i
                                WHERE i.workflow_task_uuid=t.uuid AND i.stage!='RESOLVED')
                AND NOT EXISTS (SELECT 1 FROM workflow_manual_confirmation c
                                WHERE c.workflow_task_uuid=t.uuid AND c.status IN ('pending','approved'))""",
                identities,
            ).fetchall()
        for row in rows:
            job = self._job_row(row)
            if row["task_attention_reason"] in (None, "", job.get("uncertainty_reason")):
                resolved[str(row["workflow_task_uuid"])] = bool(job.get("result_reviewed"))
        return resolved

    def list_execution_admission_blockers(
        self,
        *,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """读取当前仍占用执行准入的事实。

        参数：``limit`` 限制返回给控制台的诊断数量。返回：仍在运行或尚未完成
        清理的 Task 身份、业务状态与清理状态。异常：``limit`` 非正数时抛出
        ``ValueError``。状态不变量：查询只读，不改变 Task、Job 或资源占用事实。
        """

        if limit <= 0:
            raise ValueError("limit 必须大于零")
        blockers: list[sqlite3.Row] = []
        cursor: tuple[str, str] | None = None
        page_size = max(20, limit)
        with self._lock:
            while len(blockers) < limit:
                after = "AND (create_time, uuid) > (?, ?)" if cursor else ""
                parameters = (*cursor, page_size) if cursor else (page_size,)
                rows = self._conn.execute(
                    f"""SELECT uuid, status, cleanup_status, workflow_uuid,
                               execution_kind, create_time
                    FROM workflow_task
                    WHERE deleted_at IS NULL
                      AND (status IN ('pending', 'running', 'canceling')
                           OR cleanup_status NOT IN ('none', 'settled'))
                      {after}
                    ORDER BY create_time, uuid LIMIT ?""",
                    parameters,
                ).fetchall()
                if not rows:
                    break
                resolved = self.manual_review_attention_resolved_for_tasks(row["uuid"] for row in rows)
                blockers.extend(row for row in rows if not resolved.get(row["uuid"], False))
                cursor = (str(rows[-1]["create_time"]), str(rows[-1]["uuid"]))
                if len(rows) < page_size:
                    break
        return [
            {
                "task_uuid": str(row["uuid"]),
                "status": str(row["status"]),
                "cleanup_status": str(row["cleanup_status"]),
                "workflow_uuid": str(row["workflow_uuid"]),
                "execution_kind": str(row["execution_kind"] or "workflow"),
            }
            for row in blockers[:limit]
        ]

    def list_task_presentations(
        self,
        *,
        page: int,
        page_size: int,
        workflow_uuid: Optional[str] = None,
        execution_kind: str = "",
        status: str = "",
        cleanup_status: str = "",
        view: str = "",
        terminal_limit: int = 20,
    ) -> Dict[str, Any]:
        """分页读取 Edge 控制台需要的紧凑 Task 冻结事实。

        查询在 SQLite JSON 层裁剪大体积工作流快照与执行计划，避免先把完整图、
        参数 Schema 和执行策略解码成 Python 对象后再丢弃。返回只读展示投影，
        筛选和分页语义与 ``list_tasks`` 一致；``view=matrix`` 返回活动窗口并仅
        保留矩阵绘制、等待原因和状态计算所需字段，节点证据由详情接口按需读取。
        """

        clauses = ["task.deleted_at IS NULL"]
        values: List[Any] = []
        for field, value in (
            ("workflow_uuid", workflow_uuid),
            ("execution_kind", execution_kind),
            ("status", status),
            ("cleanup_status", cleanup_status),
        ):
            if value:
                clauses.append(f"task.{field} = ?")
                values.append(value)
        if view == "matrix":
            recent_clauses = ["recent.deleted_at IS NULL"]
            recent_values: List[Any] = []
            for field, value in (
                ("workflow_uuid", workflow_uuid),
                ("execution_kind", execution_kind),
            ):
                if value:
                    recent_clauses.append(f"recent.{field} = ?")
                    recent_values.append(value)
            clauses.append(
                """
                (
                    task.status IN ('pending', 'running', 'canceling')
                    OR task.cleanup_status = 'requires_attention'
                    OR task.uuid IN (
                        SELECT recent.uuid
                        FROM workflow_task AS recent
                        WHERE {}
                          AND recent.cleanup_status <> 'requires_attention'
                          AND recent.status IN (
                              'succeeded', 'failed', 'canceled', 'timeout'
                          )
                        ORDER BY COALESCE(
                            recent.finished_at,
                            recent.update_time,
                            recent.create_time
                        ) DESC, recent.uuid DESC
                        LIMIT ?
                    )
                )
                """.format(" AND ".join(recent_clauses))
            )
            values.extend(recent_values)
            values.append(terminal_limit)
        where = " AND ".join(clauses)
        offset = (page - 1) * page_size
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM workflow_task AS task WHERE {where}",
                values,
            ).fetchone()[0]
            pagination = "" if view == "matrix" else "LIMIT ? OFFSET ?"
            row_values = values if view == "matrix" else [*values, page_size, offset]
            rows = self._conn.execute(
                f"""
                SELECT
                    task.uuid,
                    task.create_time,
                    task.update_time,
                    task.description,
                    task.meta_data,
                    task.workflow_uuid,
                    task.execution_kind,
                    task.priority,
                    task.status,
                    json_object(
                        'workflow', json_object(
                            'uuid', json_extract(
                                task.workflow_snapshot, '$.workflow.uuid'
                            ),
                            'name', json_extract(
                                task.workflow_snapshot, '$.workflow.name'
                            ),
                            'revision', json_extract(
                                task.workflow_snapshot, '$.workflow.revision'
                            )
                        )
                    ) AS workflow_snapshot,
                    json_object(
                        'run_mode', json_extract(
                            task.execution_plan, '$.run_mode'
                        ),
                        'target_node_uuid', json_extract(
                            task.execution_plan, '$.target_node_uuid'
                        ),
                        'nodes', json(COALESCE((
                            SELECT json_group_array(json_object(
                                'uuid', json_extract(node.value, '$.uuid'),
                                'name', json_extract(node.value, '$.name'),
                                'kind', json_extract(node.value, '$.kind'),
                                'type', json_extract(node.value, '$.type'),
                                'action_name', json_extract(
                                    node.value, '$.action_name'
                                ),
                                'action_type', json_extract(
                                    node.value, '$.action_type'
                                ),
                                'device_id', json_extract(
                                    node.value, '$.device_id'
                                ),
                                'material_uuid', json_extract(
                                    node.value, '$.material_uuid'
                                ),
                                'topological_index', json_extract(
                                    node.value, '$.topological_index'
                                ),
                                'disabled', json_extract(
                                    node.value, '$.disabled'
                                )
                            ))
                            FROM json_each(task.execution_plan, '$.nodes') AS node
                        ), '[]')),
                        'edges', json(COALESCE((
                            SELECT json_group_array(json_object(
                                'uuid', json_extract(edge.value, '$.uuid'),
                                'source_node_uuid', json_extract(
                                    edge.value, '$.source_node_uuid'
                                ),
                                'target_node_uuid', json_extract(
                                    edge.value, '$.target_node_uuid'
                                )
                            ))
                            FROM json_each(task.execution_plan, '$.edges') AS edge
                        ), '[]'))
                    ) AS execution_plan,
                    task.run_mode,
                    task.execution_mode,
                    task.dispatch_gate,
                    task.target_node_uuid,
                    task.control_status,
                    task.cleanup_status,
                    task.wait_reason,
                    task.trace_context,
                    json_extract(
                        task.workflow_snapshot,
                        '$.workflow.meta_data.unilab.input_contract.parameters'
                    ) AS input_contract_parameters,
                    task.input AS task_input_source,
                    json_object(
                        'sample_id', json_extract(task.input, '$.sample_id'),
                        'sample', json_extract(task.input, '$.sample')
                    ) AS input,
                    '[]' AS error_info,
                    task.attention_reason,
                    task.started_at,
                    task.finished_at
                FROM workflow_task AS task
                WHERE {where}
                ORDER BY task.create_time DESC, task.uuid
                {pagination}
                """,
                row_values,
            ).fetchall()
        return {
            "items": [self._task_presentation_row(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": len(rows) if view == "matrix" else page_size,
        }

    def list_recoverable_tasks(
        self,
        *,
        statuses: Iterable[str] = ("pending", "running", "canceling"),
    ) -> List[Dict[str, Any]]:
        """按持久创建顺序返回启动扫描需要处理的 Task。

        参数：``statuses`` 是调用方明确允许恢复的非终态集合。返回：按
        ``create_time, uuid`` 升序排列的完整任务投影；该顺序同时是容量等待的
        稳定公平顺序，进程重启不会改变。异常：空状态集合返回空数组。
        """

        normalized = tuple(dict.fromkeys(str(status).strip() for status in statuses))
        normalized = tuple(status for status in normalized if status)
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_task
                WHERE deleted_at IS NULL AND status IN ({placeholders})
                ORDER BY create_time ASC, uuid ASC
                """,
                normalized,
            ).fetchall()
        return [self._task_row(row) for row in rows if not self.execution_held(row["uuid"])]

    def list_execution_restart_candidates(self) -> List[Dict[str, Any]]:
        """返回 Runtime 崩溃后仍需失败收敛的 Task。

        Task 自身非终态，或其父状态虽已失败/取消/超时但仍有 pending、在途 Job，
        都属于重启候选。这样并行分支先失败父任务时，剩余物理动作不会被终态
        Task 状态遮蔽。完整终态历史不进入扫描。
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT task.*
                FROM workflow_task AS task
                WHERE task.deleted_at IS NULL
                  AND (
                    task.status IN ('pending', 'running', 'canceling')
                    OR EXISTS (
                        SELECT 1
                        FROM workflow_node_job AS job
                        WHERE job.workflow_task_uuid = task.uuid
                          AND job.deleted_at IS NULL
                          AND job.status IN (
                              'pending', 'dispatched', 'running',
                              'cancel_requested'
                          )
                    )
                  )
                ORDER BY task.create_time ASC, task.uuid ASC
                """
            ).fetchall()
        return [self._task_row(row) for row in rows if not self.execution_held(row["uuid"])]

    def list_drain_blocking_job_ids(self) -> set[str]:
        """一次查询排空阻塞事实，避免为每个历史 Job 开启一个数据库事务。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT job.uuid FROM workflow_node_job AS job
                JOIN workflow_task AS task ON task.uuid = job.workflow_task_uuid
                WHERE task.deleted_at IS NULL AND job.deleted_at IS NULL
                  AND job.executor_kind <> 'material_source'
                  AND (job.status IN ('dispatched', 'running', 'cancel_requested')
                    OR EXISTS (
                        SELECT 1 FROM execution_lock_lease AS lease
                        WHERE lease.workflow_node_job_uuid = job.uuid
                          AND lease.deleted_at IS NULL AND lease.state = 'uncertain'
                    ))"""
            ).fetchall()
        return {str(row[0]) for row in rows}

    def list_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        self.get_task(task_uuid)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                ORDER BY topological_index, create_time, uuid
                """,
                (task_uuid,),
            ).fetchall()
        return [self._job_row(row) for row in rows]

    def list_jobs_for_tasks(
        self,
        task_uuids: Iterable[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量读取多个 Task 的 Job，供紧凑运行态投影消除 N+1 查询。

        参数：``task_uuids`` 是已经由任务分页查询验证存在的稳定身份。返回按 Task
        UUID 分组且保持拓扑顺序的 Job 投影；空集合返回空字典。异常：SQLite 查询
        或持久 JSON 解码错误原样传播，不把损坏事实伪装成空作业列表。
        """

        identities = tuple(dict.fromkeys(str(value) for value in task_uuids if value))
        if not identities:
            return {}
        placeholders = ", ".join("?" for _identity in identities)
        # 完整证据只在进程内计算核对 hash，不把派发凭据加入紧凑响应。
        review_fields = (*MANUAL_REVIEW_BASIS_FIELDS, "control_data", "execution_source")
        review_basis_sql = ", ".join(
            f"'{field}', " + (f"json({field})" if field in MANUAL_REVIEW_JSON_FIELDS else field)
            for field in review_fields
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT current_attempt, retry_of_job_uuid, execution_source, start_state,
                    CASE WHEN execution_source='error_manual'
                        THEN json_object({review_basis_sql}) ELSE NULL END AS manual_review_basis,
                    uuid,
                    create_time,
                    update_time,
                    workflow_task_uuid,
                    workflow_node_uuid,
                    topological_index,
                    executor_kind,
                    status,
                    attempt,
                    json_object(
                        'actual_executor', json_extract(
                            control_data, '$.actual_executor'
                        )
                    ) AS control_data,
                    '[]' AS error_info,
                    wait_reason,
                    json_object(
                        'material_uuid', json_extract(
                            expected_change_set, '$.material_uuid'
                        )
                    ) AS expected_change_set,
                    material_uuid,
                    uncertainty_reason,
                    started_at,
                    finished_at
                FROM workflow_node_job
                WHERE workflow_task_uuid IN ({placeholders}) AND deleted_at IS NULL
                ORDER BY workflow_task_uuid, topological_index, create_time, uuid
                """,
                identities,
            ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {
            identity: [] for identity in identities
        }
        for row in rows:
            grouped[str(row["workflow_task_uuid"])].append(
                self._job_presentation_row(row)
            )
        return grouped

    def get_job(self, job_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_node_job
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (job_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow node job {job_uuid} not found")
        return self._job_row(row)

    def update_pending_job_param(
        self,
        task_uuid: str,
        job_uuid: str,
        param: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """原子保存暂停任务中尚未派发 Job 的参数。

        参数：``task_uuid`` 和 ``job_uuid`` 必须指向同一个任务；``param`` 是已经
        合并好的 JSON 对象。返回更新后的 Job 投影。异常：任务未暂停、任务已经
        终态、作业不属于任务、作业不是当前 attempt 或已经离开 pending 状态时抛
        ``StoreConflict``；不存在的身份抛 ``StoreNotFound``。该方法不修改冻结
        图结构、连线、资源计划或作业身份。
        """

        if not isinstance(param, Mapping):
            raise StoreConflict("作业参数必须是对象")
        normalized_param = deepcopy(dict(param))
        now = utc_now()
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if task is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if task["status"] in {"succeeded", "success", "failed", "canceled", "timeout"}:
                raise StoreConflict("终态任务不能修改参数")
            if (
                str(task["dispatch_gate"] or "active") != "paused"
                or str(task["execution_mode"] or task["run_mode"] or "normal") != "normal"
                or str(task["control_status"] or "active") != "paused"
            ):
                raise StoreConflict("任务必须先暂停才能修改参数")
            job = conn.execute(
                """
                SELECT * FROM workflow_node_job
                WHERE uuid = ? AND workflow_task_uuid = ? AND deleted_at IS NULL
                """,
                (job_uuid, task_uuid),
            ).fetchone()
            if job is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if not bool(job["current_attempt"]):
                raise StoreConflict("历史作业 attempt 不能修改参数")
            if str(job["status"]) != "pending":
                raise StoreConflict("作业已经离开待运行状态，不能修改参数")
            updated = conn.execute(
                """
                UPDATE workflow_node_job
                SET param = ?, update_time = ?
                WHERE uuid = ? AND workflow_task_uuid = ?
                  AND deleted_at IS NULL AND current_attempt = 1
                  AND status = 'pending'
                """,
                (_json(normalized_param), now, job_uuid, task_uuid),
            ).rowcount
            if updated != 1:
                raise StoreConflict("作业状态已变化，请刷新后重试")
            updated_job = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid = ?",
                (job_uuid,),
            ).fetchone()
            assert updated_job is not None
            from unilabos.workflow.station_status_projection import append_job_state_event

            self._append_runtime_event(
                conn,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                # 运行日志的 kind 是持久化闭集；参数编辑沿用 Job 状态事件
                # 通道，通过 data.event 标记具体动作，避免为一次可重放的
                # pending→pending 修改扩大 SQLite schema 的兼容面。
                kind="job_transition",
                now=now,
                data={"event": "param_updated", "param": normalized_param},
            )
            append_job_state_event(
                conn,
                job_row=updated_job,
                status="pending",
                event_name="param_updated",
                details={"param": normalized_param},
            )
            self._append_event(
                conn,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid, "workflow_node_job_uuid": job_uuid},
                now=now,
            )
            return self._job_row(updated_job)

    def create_task_command(
        self,
        *,
        task_uuid: str,
        command_uuid: str,
        command_type: str,
        target_node_uuid: Optional[str],
        idempotency_key: str,
        description: Optional[str],
        meta_data: Dict[str, Any],
    ) -> tuple[Dict[str, Any], bool]:
        """幂等创建工作流任务控制命令。"""

        now = utc_now()
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT uuid FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if task is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            existing = conn.execute(
                """
                SELECT * FROM workflow_task_command
                WHERE workflow_task_uuid = ? AND idempotency_key = ?
                  AND deleted_at IS NULL
                """,
                (task_uuid, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["type"]) != command_type
                    or (existing["target_node_uuid"] or None)
                    != (target_node_uuid or None)
                ):
                    raise StoreConflict("同一任务控制幂等键对应的命令内容已变化")
                return self._task_command_row(existing), False
            conn.execute(
                """
                INSERT INTO workflow_task_command(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, type, target_node_uuid,
                    idempotency_key, status, result, trace_context, consumed_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'pending', '{}', '{}', NULL)
                """,
                (
                    command_uuid,
                    now,
                    now,
                    description,
                    _json(meta_data),
                    task_uuid,
                    command_type,
                    target_node_uuid,
                    idempotency_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM workflow_task_command WHERE uuid = ?",
                (command_uuid,),
            ).fetchone()
            return self._task_command_row(row), True

    def set_task_control_status(
        self,
        task_uuid: str,
        *,
        control_status: str,
    ) -> Dict[str, Any]:
        """Persist the Agent-visible pause/resume state on the standard Task."""

        if control_status not in {"active", "paused"}:
            raise StoreConflict("任务控制状态非法")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if row["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                raise StoreConflict("终态任务不能修改控制状态")
            previous = str(row["control_status"])
            requested_gate = control_status if control_status in {"active", "paused"} else None
            if previous == "error_waiting":
                control_status = "error_waiting"
            current_gate = str(row["dispatch_gate"] or "active")
            changed = previous != control_status or (
                requested_gate is not None and current_gate != requested_gate
            )
            if changed:
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = ?,
                        dispatch_gate = COALESCE(?, dispatch_gate),
                        update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (control_status, requested_gate, now, task_uuid),
                )
                self._append_event(
                    conn,
                    event="workflow.runtime.changed",
                    data={"workflow_task_uuid": task_uuid},
                    now=now,
                )
            updated = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ?", (task_uuid,)
            ).fetchone()
            return self._task_row(updated)

    def set_task_dispatch_gate(
        self,
        task_uuid: str,
        *,
        dispatch_gate: str,
    ) -> Dict[str, Any]:
        """原子修改普通 Task 的派发闸门，不改变单步执行模式。

        ``dispatch_gate`` 是操作员意图；当任务正在等待错误处置或物理对账时，
        对外 ``control_status`` 继续保留更高优先级的等待状态，但新的闸门意图
        会被保存，待处置结束后按该意图恢复。
        """

        if dispatch_gate not in {"active", "paused"}:
            raise StoreConflict("任务派发闸门状态非法")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if row["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                raise StoreConflict("终态任务不能修改派发闸门")
            current_gate = str(row["dispatch_gate"] or "active")
            current_status = str(row["control_status"] or "active")
            visible_status = (
                dispatch_gate
                if current_status in {"active", "paused"}
                else current_status
            )
            changed = current_gate != dispatch_gate or current_status != visible_status
            if changed:
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET dispatch_gate = ?, control_status = ?,
                        reconciliation_resume_control_status = CASE
                            WHEN control_status = 'waiting_reconciliation' THEN ?
                            ELSE reconciliation_resume_control_status
                        END,
                        update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (dispatch_gate, visible_status, dispatch_gate, now, task_uuid),
                )
                # 如果任务正在等待标准人工干预，新的操作员意图也要写入尚未
                # 关闭的干预记录，避免交付成功后恢复到旧状态。
                conn.execute(
                    """
                    UPDATE workflow_intervention
                    SET resume_control_status = ?, update_time = ?
                    WHERE workflow_task_uuid = ? AND status IN ('open', 'selected')
                    """,
                    (dispatch_gate, now, task_uuid),
                )
                self._append_event(
                    conn,
                    event="workflow.runtime.changed",
                    data={
                        "workflow_task_uuid": task_uuid,
                        "dispatch_gate": dispatch_gate,
                    },
                    now=now,
                )
            updated = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ?", (task_uuid,)
            ).fetchone()
            return self._task_row(updated)

    def set_task_execution_mode(
        self,
        task_uuid: str,
        *,
        execution_mode: str,
        control_status: str,
        dispatch_gate: str | None = None,
    ) -> Dict[str, Any]:
        """原子保存调试执行模式及其派发闸门状态。"""

        if execution_mode not in {"normal", "switching_to_step", "step"}:
            raise StoreConflict("任务执行模式非法")
        if control_status not in {"active", "paused"}:
            raise StoreConflict("任务控制状态非法")
        if dispatch_gate is None:
            dispatch_gate = (
                "paused" if execution_mode in {"switching_to_step", "step"} else "active"
            )
        if dispatch_gate not in {"active", "paused"}:
            raise StoreConflict("任务派发闸门状态非法")
        if execution_mode in {"switching_to_step", "step"}:
            dispatch_gate = "paused"
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if row["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                raise StoreConflict("终态任务不能修改执行模式")
            if execution_mode in {"switching_to_step", "step"}:
                blocker = self._execution_admission_blocker(
                    conn,
                    requested_run_mode="step",
                    exclude_task_uuid=task_uuid,
                )
                if blocker is not None:
                    raise StoreConflict(
                        "debug_task_conflict:"
                        f"{blocker['uuid']}:{blocker['status']}"
                    )
            # 用户执行模式与未决错误各自保存；旧 pause/resume 不解除错误闸门。
            if row["control_status"] == "error_waiting":
                control_status = "error_waiting"
            if (
                str(row["execution_mode"]) != execution_mode
                or str(row["control_status"]) != control_status
                or str(row["dispatch_gate"] or "active") != dispatch_gate
            ):
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET execution_mode = ?, control_status = ?, dispatch_gate = ?,
                        update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (execution_mode, control_status, dispatch_gate, now, task_uuid),
                )
                self._append_event(
                    conn,
                    event="workflow.runtime.changed",
                    data={"workflow_task_uuid": task_uuid},
                    now=now,
                )
            updated = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ?", (task_uuid,)
            ).fetchone()
            return self._task_row(updated)

    def complete_task_command(
        self,
        command_uuid: str,
        *,
        status: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """把已接收控制命令原子标记为成功或拒绝。"""

        if status not in {"succeeded", "rejected"}:
            raise StoreConflict("任务命令终态非法")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_task_command
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (command_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task command {command_uuid} not found")
            if row["status"] != "pending":
                return self._task_command_row(row)
            conn.execute(
                """
                UPDATE workflow_task_command
                SET status = ?, result = ?, consumed_at = ?, update_time = ?
                WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                """,
                (status, _json(result), now, now, command_uuid),
            )
            self._append_runtime_event(
                conn,
                task_uuid=str(row["workflow_task_uuid"]),
                command_uuid=command_uuid,
                kind="command_consumed",
                now=now,
                data={"type": row["type"], "status": status, "result": result},
            )
            updated = conn.execute(
                "SELECT * FROM workflow_task_command WHERE uuid = ?",
                (command_uuid,),
            ).fetchone()
            return self._task_command_row(updated)

    # Workflow Debugger -------------------------------------------------

    def create_debug_configuration(
        self,
        *,
        task_uuid: str,
        start_node_uuids: list[str],
        breakpoint_node_uuids: list[str],
    ) -> Dict[str, Any]:
        """冻结调试配置并在首个活动作业前建立持久 Hold。"""

        now = utc_now()
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT uuid, debug_create_time FROM workflow_task "
                "WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if task is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if task["debug_create_time"] is not None:
                raise sqlite3.IntegrityError(
                    f"debug configuration {task_uuid} already exists"
                )
            conn.execute(
                """
                UPDATE workflow_task
                SET debug_create_time = ?, debug_update_time = ?,
                    debug_start_node_uuids = ?, debug_breakpoint_node_uuids = ?,
                    debug_execution_policy = 'step', debug_status = 'paused'
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (
                    now,
                    now,
                    _json(start_node_uuids),
                    _json(breakpoint_node_uuids),
                    task_uuid,
                ),
            )
            # MaterialSource 作业会被保留以完成任务级物料准入，但调试首个 Hold
            # 必须落在用户选择的可执行起点，不能退化为“第一个拓扑作业”。
            first_job = conn.execute(
                """
                SELECT uuid, workflow_node_uuid, attempt
                FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                  AND workflow_node_uuid = ?
                  AND status = 'pending'
                LIMIT 1
                """,
                (task_uuid, start_node_uuids[0]),
            ).fetchone()
            if first_job is None:
                raise StoreConflict("debug task has no active job")
            self._insert_debug_hold(
                conn,
                task_uuid=task_uuid,
                job_row=first_job,
                reason="start",
                now=now,
            )
            self._append_event(
                conn,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )
        return self.get_debug_projection(task_uuid)

    def get_debug_projection(self, task_uuid: str) -> Dict[str, Any]:
        """读取不可变配置和完整 Hold 历史。"""

        with self._lock:
            configuration = self._conn.execute(
                """
                SELECT debug_create_time AS create_time,
                       debug_update_time AS update_time,
                       debug_start_node_uuids AS start_node_uuids,
                       debug_breakpoint_node_uuids AS breakpoint_node_uuids,
                       debug_execution_policy AS execution_policy,
                       debug_status AS status
                FROM workflow_task
                WHERE uuid = ? AND debug_create_time IS NOT NULL
                """,
                (task_uuid,),
            ).fetchone()
            if configuration is None:
                raise StoreNotFound(f"debug configuration {task_uuid} not found")
            holds = self._conn.execute(
                """
                SELECT * FROM workflow_node_admission_hold
                WHERE workflow_task_uuid = ?
                ORDER BY create_time, uuid
                """,
                (task_uuid,),
            ).fetchall()
        return {
            "configuration": {
                "start_node_uuids": _load(configuration["start_node_uuids"], []),
                "breakpoint_node_uuids": _load(
                    configuration["breakpoint_node_uuids"], []
                ),
            },
            "execution_policy": configuration["execution_policy"],
            "status": configuration["status"],
            "holds": [self._debug_hold_row(row) for row in holds],
        }

    def begin_debug_command(
        self,
        *,
        task_uuid: str,
        command_uuid: str,
        command_type: str,
        hold_uuid: str,
        idempotency_key: str,
    ) -> tuple[Dict[str, Any], bool, str]:
        """幂等接收命令、核对精确 Hold，并在同一事务放行。"""

        if command_type not in {"step", "continue"}:
            raise StoreConflict("invalid debug command")
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM workflow_task_debug_command
                WHERE workflow_task_uuid = ? AND idempotency_key = ?
                """,
                (task_uuid, idempotency_key),
            ).fetchone()
            if existing is not None:
                hold = conn.execute(
                    "SELECT workflow_node_uuid FROM workflow_node_admission_hold WHERE uuid = ?",
                    (existing["hold_uuid"],),
                ).fetchone()
                if hold is None:
                    raise StoreConflict("debug command hold missing")
                return (
                    self._debug_command_row(existing),
                    False,
                    str(hold["workflow_node_uuid"]),
                )
            hold = conn.execute(
                """
                SELECT * FROM workflow_node_admission_hold
                WHERE uuid = ? AND workflow_task_uuid = ? AND status = 'open'
                """,
                (hold_uuid, task_uuid),
            ).fetchone()
            if hold is None:
                raise StoreConflict("debug hold is not open")
            conn.execute(
                """
                INSERT INTO workflow_task_debug_command(
                    uuid, create_time, update_time, workflow_task_uuid,
                    type, hold_uuid, idempotency_key, status, result, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '{}', NULL)
                """,
                (
                    command_uuid,
                    now,
                    now,
                    task_uuid,
                    command_type,
                    hold_uuid,
                    idempotency_key,
                ),
            )
            conn.execute(
                """
                UPDATE workflow_node_admission_hold
                SET status = 'released', released_at = ?, update_time = ?
                WHERE uuid = ? AND status = 'open'
                """,
                (now, now, hold_uuid),
            )
            conn.execute(
                """
                UPDATE workflow_task
                SET debug_execution_policy = ?, debug_status = 'running',
                    debug_update_time = ?
                WHERE uuid = ? AND debug_create_time IS NOT NULL
                """,
                (command_type, now, task_uuid),
            )
            row = conn.execute(
                "SELECT * FROM workflow_task_debug_command WHERE uuid = ?",
                (command_uuid,),
            ).fetchone()
            self._append_event(
                conn,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )
            return (
                self._debug_command_row(row),
                True,
                str(hold["workflow_node_uuid"]),
            )

    def complete_debug_command(
        self,
        command_uuid: str,
        *,
        status: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """完成调试命令；重复调用返回既有终态。"""

        if status not in {"succeeded", "rejected"}:
            raise StoreConflict("invalid debug command status")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_task_debug_command WHERE uuid = ?",
                (command_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"debug command {command_uuid} not found")
            if row["status"] == "pending":
                conn.execute(
                    """
                    UPDATE workflow_task_debug_command
                    SET status = ?, result = ?, consumed_at = ?, update_time = ?
                    WHERE uuid = ? AND status = 'pending'
                    """,
                    (status, _json(result), now, now, command_uuid),
                )
            updated = conn.execute(
                "SELECT * FROM workflow_task_debug_command WHERE uuid = ?",
                (command_uuid,),
            ).fetchone()
            return self._debug_command_row(updated)

    def advance_debug_after_job_finished(self, task_uuid: str) -> Dict[str, Any]:
        """按调试策略为下一个待处理作业建 Hold，或请求继续派发。"""

        now = utc_now()
        with self.transaction() as conn:
            configuration = conn.execute(
                """
                SELECT debug_create_time AS create_time,
                       debug_update_time AS update_time,
                       debug_start_node_uuids AS start_node_uuids,
                       debug_breakpoint_node_uuids AS breakpoint_node_uuids,
                       debug_execution_policy AS execution_policy,
                       debug_status AS status
                FROM workflow_task
                WHERE uuid = ? AND debug_create_time IS NOT NULL
                """,
                (task_uuid,),
            ).fetchone()
            if configuration is None:
                return {"type": "none"}
            task = conn.execute(
                "SELECT status FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if task is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if task["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET debug_status = 'completed', debug_update_time = ?
                    WHERE uuid = ? AND debug_create_time IS NOT NULL
                    """,
                    (now, task_uuid),
                )
                return {"type": "complete"}
            pending = conn.execute(
                """
                SELECT uuid, workflow_node_uuid, attempt
                FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                  AND status = 'pending'
                ORDER BY topological_index, create_time, uuid
                LIMIT 1
                """,
                (task_uuid,),
            ).fetchone()
            if pending is None:
                return {"type": "none"}
            breakpoints = set(_load(configuration["breakpoint_node_uuids"], []))
            node_uuid = str(pending["workflow_node_uuid"])
            should_hold = (
                configuration["execution_policy"] == "step" or node_uuid in breakpoints
            )
            if not should_hold:
                return {"type": "step", "workflow_node_uuid": node_uuid}
            open_hold = conn.execute(
                """
                SELECT uuid FROM workflow_node_admission_hold
                WHERE workflow_task_uuid = ? AND status = 'open'
                """,
                (task_uuid,),
            ).fetchone()
            if open_hold is None:
                self._insert_debug_hold(
                    conn,
                    task_uuid=task_uuid,
                    job_row=pending,
                    reason=("breakpoint" if node_uuid in breakpoints else "step"),
                    now=now,
                )
            conn.execute(
                """
                UPDATE workflow_task
                SET debug_status = 'paused', debug_update_time = ?
                WHERE uuid = ? AND debug_create_time IS NOT NULL
                """,
                (now, task_uuid),
            )
            self._append_event(
                conn,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )
            return {"type": "hold", "workflow_node_uuid": node_uuid}

    def stop_debug(self, task_uuid: str) -> None:
        """关闭调试配置并取消尚未放行的 Hold；普通任务调用是幂等空操作。"""

        now = utc_now()
        with self.transaction() as conn:
            configuration = conn.execute(
                """
                SELECT uuid FROM workflow_task
                WHERE uuid = ? AND debug_create_time IS NOT NULL
                """,
                (task_uuid,),
            ).fetchone()
            if configuration is None:
                return
            conn.execute(
                """
                UPDATE workflow_task
                SET debug_status = 'stopped', debug_update_time = ?
                WHERE uuid = ? AND debug_create_time IS NOT NULL
                """,
                (now, task_uuid),
            )
            conn.execute(
                """
                UPDATE workflow_node_admission_hold
                SET status = 'canceled', update_time = ?
                WHERE workflow_task_uuid = ? AND status = 'open'
                """,
                (now, task_uuid),
            )
            self._append_event(
                conn,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )

    @staticmethod
    def _insert_debug_hold(
        conn: sqlite3.Connection,
        *,
        task_uuid: str,
        job_row: sqlite3.Row,
        reason: str,
        now: str,
    ) -> str:
        hold_uuid = str(uuid4())
        conn.execute(
            """
            INSERT INTO workflow_node_admission_hold(
                uuid, create_time, update_time, workflow_task_uuid,
                workflow_node_job_uuid, workflow_node_uuid, attempt,
                reason, status, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL)
            """,
            (
                hold_uuid,
                now,
                now,
                task_uuid,
                job_row["uuid"],
                job_row["workflow_node_uuid"],
                int(job_row["attempt"]),
                reason,
            ),
        )
        return hold_uuid

    @staticmethod
    def _debug_hold_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            "uuid": row["uuid"],
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            "workflow_node_uuid": row["workflow_node_uuid"],
            "attempt": row["attempt"],
            "reason": row["reason"],
            "status": row["status"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
        }
        if row["released_at"] is not None:
            result["released_at"] = row["released_at"]
        return result

    @staticmethod
    def _debug_command_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            "uuid": row["uuid"],
            "workflow_task_uuid": row["workflow_task_uuid"],
            "type": row["type"],
            "scope": {"type": "hold", "hold_uuid": row["hold_uuid"]},
            "idempotency_key": row["idempotency_key"],
            "status": row["status"],
            "result": _load(row["result"], {}),
            "create_time": row["create_time"],
            "update_time": row["update_time"],
        }
        if row["consumed_at"] is not None:
            result["consumed_at"] = row["consumed_at"]
        return result

    def list_task_runtime_events(
        self,
        task_uuid: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> Dict[str, Any]:
        """读取一次工作流任务的持久运行时间线。

        参数：``task_uuid`` 是工作流任务身份；``after_sequence`` 是排他全局序号；
        ``limit`` 是公开页长。返回按序号递增的运行事件页，并把作业下发参数与
        明确执行结果补充到对应状态转换。异常：任务不存在时抛 ``StoreNotFound``。
        """

        self.get_task(task_uuid)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    journal.sequence AS event_sequence,
                    journal.workflow_task_uuid AS event_task_uuid,
                    journal.workflow_node_job_uuid AS event_job_uuid,
                    journal.workflow_task_command_uuid AS event_command_uuid,
                    journal.kind AS event_kind,
                    journal.from_status AS event_from_status,
                    journal.to_status AS event_to_status,
                    journal.data AS event_data,
                    journal.create_time AS event_create_time,
                    job.workflow_node_uuid AS job_workflow_node_uuid,
                    job.executor_kind AS job_executor_kind,
                    job.attempt AS job_attempt,
                    job.param AS job_param,
                    job.return_info AS job_return_info,
                    job.error_info AS job_error_info
                FROM workflow_runtime_journal AS journal
                LEFT JOIN workflow_node_job AS job
                  ON job.uuid = journal.workflow_node_job_uuid
                 AND job.deleted_at IS NULL
                WHERE journal.workflow_task_uuid = ?
                  AND journal.sequence > ?
                ORDER BY journal.sequence
                LIMIT ?
                """,
                (task_uuid, after_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        return {
            "items": [self._runtime_event_row(row) for row in selected],
            "next_cursor": (
                selected[-1]["event_sequence"] if selected else after_sequence
            ),
            "has_more": has_more,
        }

    def get_node_template(self, template_uuid: str) -> Dict[str, Any]:
        """读取一个活动工作流节点模板（WorkflowNodeTemplate）。

        参数：``template_uuid`` 是已发布模板的稳定 UUID。返回 Backend-shaped
        模板投影；模板不存在或已软删除时抛出 ``StoreNotFound``。
        """

        if self._template_snapshot_provider is not None:
            try:
                return (
                    self._template_snapshot_provider.snapshot()
                    .require_template(template_uuid)
                    .detached_template()
                )
            except AuthoringCatalogError as error:
                persisted = self._published_template_entities(template_uuid)
                if persisted is None:
                    raise StoreNotFound(
                        f"workflow node template {template_uuid} not found"
                    ) from error
                return persisted[0]
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_node_template
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (template_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow node template {template_uuid} not found")
        return self._node_template_row(row)

    def list_device_action_templates(self, resource_template_uuid: str) -> list[dict[str, Any]]:
        """从当前权威目录读取设备类型的全部动作，不按运行占用过滤。"""
        if self._template_snapshot_provider is not None:
            templates = [entry.detached_template() for entry in self._template_snapshot_provider.snapshot().actions]
        else:
            with self.read() as conn:
                templates = [self._node_template_row(row) for row in conn.execute(
                    "SELECT * FROM workflow_node_template WHERE resource_template_uuid=? AND deleted_at IS NULL", (resource_template_uuid,))]
        return [item for item in templates if item.get("resource_template_uuid") == resource_template_uuid and str(item.get("node_type", "")).lower() == "ilab"]

    # Authoring ----------------------------------------------------------

    def install_discovered_sources(
        self,
        registrations: Iterable[Mapping[str, str]],
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        """以单事务安装定义、来源和空工作流创作（Authoring）事实。

        参数：``registrations`` 是已完成文件系统校验的完整来源集合；
        ``before_commit`` 在所有 SQL 写入后、事务提交前复核外部目录身份。
        返回：按输入顺序排列的持久注册记录。
        异常：任一工作流生命周期、物理路径、来源 URI 或包身份冲突抛出
        ``StoreConflict``；提交前复核异常原样传播，整个事务不提交。
        """

        return self._commit_source_registrations(
            registrations,
            before_commit=before_commit,
            allow_create_missing=True,
        )

    def register_sources(
        self,
        registrations: Iterable[Mapping[str, str]],
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        """兼容旧调用并委托工作流源码定义安装深模块（Deep Module）。

        参数：``registrations`` 与 ``before_commit`` 保持旧接口含义。返回：完整安装
        后的来源注册行；异常语义与 ``install_discovered_sources`` 相同。
        """

        return self._commit_source_registrations(
            registrations,
            before_commit=before_commit,
            allow_create_missing=False,
        )

    def _commit_source_registrations(
        self,
        registrations: Iterable[Mapping[str, str]],
        *,
        before_commit: Callable[[], None] | None,
        allow_create_missing: bool,
    ) -> list[dict[str, Any]]:
        """在唯一 ``BEGIN IMMEDIATE`` 接缝（Seam）调用源码启动深模块（Deep Module）。

        参数：``registrations`` 是完整来源批次；``before_commit`` 是固定包根复核；
        ``allow_create_missing`` 区分显式安装与旧兼容入口。返回：提交后的注册行。
        异常：深模块冲突统一映射为 ``StoreConflict``，SQLite 唯一冲突整体回滚。
        """

        try:
            with self.transaction() as conn:
                return source_bootstrap.install_discovered_sources(
                    conn,
                    registrations,
                    now=utc_now(),
                    before_commit=before_commit,
                    allow_create_missing=allow_create_missing,
                )
        except source_bootstrap.SourceBootstrapConflict as exc:
            raise StoreConflict(str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise StoreConflict("工作流源码身份已被占用") from exc

    def get_source_registration(self, workflow_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_source_registration
                WHERE workflow_uuid = ?
                """,
                (workflow_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(
                f"authoring source for workflow {workflow_uuid} is not registered"
            )
        return dict(row)

    def list_source_registrations(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT registration.*
                FROM workflow_source_registration AS registration
                JOIN workflow
                  ON workflow.uuid = registration.workflow_uuid
                WHERE workflow.deleted_at IS NULL
                ORDER BY registration.workflow_uuid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def bootstrap_workflow_revision(
        self,
        workflow_uuid: str,
        *,
        revision: int,
    ) -> bool:
        """为冷启动空工作流骨架预置已发布合同修订。

        参数：``workflow_uuid`` 是来源清单中的稳定工作流身份；``revision`` 是
        领域包发布合同固定的工作流修订。返回：目标仍是没有任何图事实的空骨架、
        且已安全采用该修订时为 ``True``；若图已有事实或当前修订更新，不覆盖并
        返回 ``False``。异常：工作流不存在或修订格式无效抛 ``StoreNotFound``/
        ``StoreConflict``；事务整体回滚。

        该接缝只移动冷启动骨架的修订基线，图、物料需求和作者源码仍由随后一次
        普通 Authoring candidate 提交。通过空骨架和“不降级修订”双重闸门，普通
        graph apply 的修订递增语义不受影响。
        """

        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise StoreConflict("冷启动发布修订格式无效")
        with self.transaction() as conn:
            workflow = self.get_workflow(workflow_uuid, conn=conn)
            # 查询全部历史行（而非只查 deleted_at IS NULL），避免把一个已有过
            # 编辑的工作流误判成可覆盖的空骨架。
            for table in (
                "workflow_node",
                "workflow_edge",
                "workflow_inventory_requirement",
            ):
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE workflow_uuid = ? LIMIT 1",
                    (workflow_uuid,),
                ).fetchone() is not None:
                    return False
            # 图为空并不足以证明这是刚由来源清单安装出的骨架：作者记录可能已经
            # 留有候选、已应用源码、草稿观测或未完成写回。若在这些事实上移动
            # revision，会把一个真实编辑中的工作流伪装成发布合同基线，随后冷启动
            # 可能覆盖/跳过其版本。只允许 ``_ensure_empty_authoring`` 写出的全空
            # 创作记录（以及尚未创建记录的兼容存储）通过此闸门。
            authoring = conn.execute(
                """
                SELECT observed_draft_hash, draft_update_time, diagnostics,
                       candidate_hash, candidate, applied_source,
                       writeback_status, writeback_source,
                       writeback_expected_hash, writeback_generation
                FROM workflow_authoring
                WHERE workflow_uuid = ?
                """,
                (workflow_uuid,),
            ).fetchone()
            if authoring is not None:
                try:
                    diagnostics = _load(authoring["diagnostics"], [])
                    candidate = _load(authoring["candidate"], None)
                    applied_source = _load(authoring["applied_source"], None)
                except (TypeError, ValueError, UnicodeError, RecursionError):
                    # 损坏的创作 JSON 交给后续正常恢复报告；这里绝不先改动版本。
                    return False
                if (
                    authoring["observed_draft_hash"] is not None
                    or authoring["draft_update_time"] is not None
                    or diagnostics != []
                    or authoring["candidate_hash"] is not None
                    or candidate is not None
                    or applied_source is not None
                    or authoring["writeback_status"] != "settled"
                    or authoring["writeback_source"] is not None
                    or authoring["writeback_expected_hash"] is not None
                    or authoring["writeback_generation"] is not None
                ):
                    return False
            current_revision = int(workflow["revision"])
            if current_revision > revision:
                return False
            if current_revision < revision:
                conn.execute(
                    "UPDATE workflow SET revision = ?, update_time = ? "
                    "WHERE uuid = ? AND deleted_at IS NULL",
                    (revision, utc_now(), workflow_uuid),
                )
            return True

    def get_authoring_record(self, workflow_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_authoring WHERE workflow_uuid = ?",
                (workflow_uuid,),
            ).fetchone()
        if row is None:
            return {
                "workflow_uuid": workflow_uuid,
                "observed_draft_hash": None,
                "draft_update_time": None,
                "diagnostics": [],
                "candidate_hash": None,
                "candidate": None,
                "applied_source": None,
                "writeback_status": "settled",
                "writeback_source": None,
                "writeback_expected_hash": None,
                "writeback_generation": None,
                "update_time": None,
            }
        result = dict(row)
        result["diagnostics"] = _load(result["diagnostics"], [])
        result["candidate"] = _load(result["candidate"], None)
        result["applied_source"] = _load(result["applied_source"], None)
        return result

    def validate_candidate_identity_ownership(
        self,
        *,
        workflow_uuid: str,
        node_uuids: Iterable[str],
        edge_uuids: Iterable[str],
    ) -> None:
        """验证候选节点和连线身份未被其他工作流占用。

        参数：``workflow_uuid`` 是候选所属工作流；``node_uuids``、``edge_uuids``
        是已完成候选结构校验的稳定身份。返回：无。异常：任一身份已属于其他
        工作流时抛 ``candidate_identity_conflict``，让服务在签发候选前暴露明确
        诊断，而不是把冲突延迟到 Apply 事务。
        """

        with self._lock:
            for table, identities in (
                ("workflow_node", node_uuids),
                ("workflow_edge", edge_uuids),
            ):
                for identity in identities:
                    owner = self._conn.execute(
                        f"SELECT workflow_uuid FROM {table} WHERE uuid = ?",
                        (identity,),
                    ).fetchone()
                    if owner is not None and owner["workflow_uuid"] != workflow_uuid:
                        raise StoreAuthoringConflict("candidate_identity_conflict")

    def record_draft_compilation(
        self,
        *,
        workflow_uuid: str,
        draft_hash: Optional[str],
        draft_update_time: Optional[str],
        diagnostics: List[Dict[str, Any]],
        candidate_hash: Optional[str],
        candidate: Optional[Dict[str, Any]],
        event_data: Dict[str, Any],
    ) -> int:
        now = utc_now()
        with self.transaction() as conn:
            self.get_workflow(workflow_uuid, conn=conn)
            conn.execute(
                """
                INSERT INTO workflow_authoring(
                    workflow_uuid, observed_draft_hash, draft_update_time,
                    diagnostics, candidate_hash, candidate, update_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_uuid) DO UPDATE SET
                    observed_draft_hash = excluded.observed_draft_hash,
                    draft_update_time = excluded.draft_update_time,
                    diagnostics = excluded.diagnostics,
                    candidate_hash = excluded.candidate_hash,
                    candidate = excluded.candidate,
                    writeback_status = 'settled',
                    writeback_source = NULL,
                    writeback_expected_hash = NULL,
                    writeback_generation = NULL,
                    update_time = excluded.update_time
                """,
                (
                    workflow_uuid,
                    draft_hash,
                    draft_update_time,
                    _json(diagnostics),
                    candidate_hash,
                    _json(candidate) if candidate is not None else None,
                    now,
                ),
            )
            return self._append_event(
                conn,
                event="workflow.authoring.changed",
                data=event_data,
                now=now,
            )

    def apply_authoring_candidate(
        self,
        *,
        workflow_uuid: str,
        candidate_hash: str,
        authoring_authority_validator: Callable[[str, str], None],
        advance_revision: bool = True,
    ) -> Tuple[int, str]:
        """在线性化写事务内应用服务端持久候选稿（Candidate）。

        参数：``workflow_uuid`` 是工作流（Workflow）身份；``candidate_hash``
        是调用者持有的服务端签发候选哈希（Candidate Hash）；
        ``authoring_authority_validator`` 在同一 ``BEGIN IMMEDIATE`` 内复核存储
        候选推导出的源码权威（Source Authority）草稿哈希与目录指纹（Catalog
        Fingerprint）；``advance_revision`` 仅供冷启动已发布合同恢复接缝，在
        已确认的空骨架合同修订上应用图时保持修订，否则必须为 ``True``。返回：
        结果工作流修订（Workflow Revision）与提交后写回世代。异常：任何候选、
        草稿、目录或修订冲突都在图、事件和写回标记写入前失败，并由事务整体回滚。
        """

        if not isinstance(advance_revision, bool):
            raise StoreConflict("工作流修订推进标志格式无效")
        now = utc_now()
        with self.transaction() as conn:
            writeback_generation = str(uuid4())
            authoring = conn.execute(
                """
                SELECT observed_draft_hash, candidate_hash, candidate
                FROM workflow_authoring
                WHERE workflow_uuid = ?
                """,
                (workflow_uuid,),
            ).fetchone()
            if authoring is None:
                raise StoreAuthoringConflict("candidate_not_ready")
            stored_candidate = _load(authoring["candidate"], None)
            if not isinstance(stored_candidate, dict):
                raise StoreAuthoringConflict("candidate_not_ready")
            try:
                # ``recomputed_candidate_hash`` 绑定事务内刚重读的完整八字段正文。
                recomputed_candidate_hash = compute_authoring_candidate_hash(
                    stored_candidate
                )
            except AuthoringCandidateHashError:
                raise StoreAuthoringConflict("candidate_hash_conflict") from None
            if (
                recomputed_candidate_hash != candidate_hash
                or authoring["candidate_hash"] != candidate_hash
                or stored_candidate.get("candidate_hash") != candidate_hash
            ):
                raise StoreAuthoringConflict("candidate_hash_conflict")
            try:
                # 事务前置条件只从同一持久候选推导，禁止客户端混搭世代。
                expected_draft_hash = stored_candidate["draft_hash"]
                expected_revision = stored_candidate["base_workflow_revision"]
                expected_catalog_fingerprint = stored_candidate[
                    "template_catalog_fingerprint"
                ]
                changeset = stored_candidate["changeset"]
                kind = changeset["kind"]
                graph = stored_candidate["graph"]
                normalized_source = stored_candidate["normalized_python_source"]
            except (KeyError, TypeError):
                raise StoreConflict("候选版本（Candidate）持久包缺少应用事实") from None
            if (
                not isinstance(expected_draft_hash, str)
                or type(expected_revision) is not int
                or expected_revision < 1
                or not isinstance(expected_catalog_fingerprint, str)
                or not isinstance(normalized_source, str)
            ):
                raise StoreConflict("候选版本（Candidate）持久包应用事实类型无效")
            if authoring["observed_draft_hash"] != expected_draft_hash:
                raise StoreAuthoringConflict("draft_hash_conflict")
            workflow = self.get_workflow(workflow_uuid, conn=conn)
            if workflow["revision"] != expected_revision:
                raise StoreRevisionConflict("workflow revision changed before apply")

            # 文件系统不能与 SQLite 共用锁；在首个领域写入前完成线性化复核。
            authoring_authority_validator(
                expected_draft_hash,
                expected_catalog_fingerprint,
            )
            candidate = stored_candidate
            if kind == "graph":
                graph_workflow = graph.get("workflow")
                if not isinstance(graph_workflow, dict):
                    raise StoreConflict("Candidate 缺少 Workflow 根对象")
                if (
                    graph_workflow.get("uuid") != workflow_uuid
                    or graph_workflow.get("revision") != expected_revision
                ):
                    raise StoreConflict("Candidate Workflow 身份或版本不匹配")
                candidate_meta = graph_workflow.get("meta_data")
                if not isinstance(candidate_meta, dict):
                    raise StoreConflict("Candidate Workflow meta_data 必须是对象")
                nodes = [
                    WorkflowNodeWrite.model_validate(
                        {
                            field: item[field]
                            for field in WorkflowNodeWrite.model_fields
                            if field in item
                        }
                    )
                    for item in graph.get("nodes", [])
                ]
                edges = [
                    WorkflowEdgeWrite.model_validate(
                        {
                            field: item[field]
                            for field in WorkflowEdgeWrite.model_fields
                            if field in item
                        }
                    )
                    for item in graph.get("edges", [])
                ]
                inventory_requirements = [
                    WorkflowInventoryRequirementWrite.model_validate(
                        {
                            field: item[field]
                            for field in WorkflowInventoryRequirementWrite.model_fields
                            if field in item
                        }
                    )
                    for item in graph.get("inventory_requirements", [])
                ]
                self._ensure_authoring_catalog_projection(
                    conn,
                    node_templates=graph.get("node_templates", []),
                    handle_templates=graph.get("handle_templates", []),
                    authority_id=(
                        "authoring/" + str(candidate["template_catalog_fingerprint"])
                    ),
                    now=now,
                )
                resulting_revision = self._reconcile_graph(
                    conn,
                    workflow_uuid=workflow_uuid,
                    expected_revision=expected_revision,
                    nodes=nodes,
                    edges=edges,
                    inventory_requirements=inventory_requirements,
                    advance_revision=advance_revision,
                    protect_reserved_metadata=False,
                    semantic_workflow_meta_data=candidate_meta,
                    validate_workflow_io_contract=True,
                )
                # 领域 Python 是工作流定义权威；候选已经由 AST 与目录固定点
                # 验证，因此公开元数据和系统生成的 ``unilab`` 元数据都以候选
                # 根对象为准，不能继续保留内存投影中的陈旧公开字段。
                workflow_meta = dict(candidate_meta)
                conn.execute(
                    """
                    UPDATE workflow
                    SET meta_data = ?, name = ?, tags = ?, description = ?,
                        workflow_type = ?, update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (
                        _json(workflow_meta),
                        graph_workflow["name"],
                        _json(graph_workflow.get("tags") or []),
                        graph_workflow.get("description"),
                        graph_workflow.get("workflow_type", "normal"),
                        now,
                        workflow_uuid,
                    ),
                )
                self._assert_graph_executable(conn, workflow_uuid)
            elif kind == "source_only":
                resulting_revision = expected_revision
            else:
                raise StoreConflict(f"unsupported Authoring changeset kind {kind!r}")
            normalized_hash = (
                "sha256:"
                + hashlib.sha256(normalized_source.encode("utf-8")).hexdigest()
            )
            applied_source = {
                "python_source": normalized_source,
                "source_hash": normalized_hash,
                "source_map": candidate["source_map"],
                "compiler_version": candidate["compiler_version"],
                "template_catalog_fingerprint": expected_catalog_fingerprint,
                "workflow_revision": resulting_revision,
                "update_time": now,
            }
            conn.execute(
                """
                UPDATE workflow_authoring
                SET diagnostics = '[]', candidate_hash = NULL,
                    candidate = NULL, applied_source = ?,
                    writeback_status = 'pending',
                    writeback_source = ?,
                    writeback_expected_hash = observed_draft_hash,
                    writeback_generation = ?,
                    update_time = ?
                WHERE workflow_uuid = ?
                """,
                (
                    _json(applied_source),
                    applied_source["python_source"],
                    writeback_generation,
                    now,
                    workflow_uuid,
                ),
            )
            self._append_event(
                conn,
                event="workflow.authoring.changed",
                data={
                    "workflow_uuid": workflow_uuid,
                    "cause": "applied",
                    "draft_hash": normalized_hash,
                    "candidate_hash": None,
                    "workflow_revision": resulting_revision,
                },
                now=now,
            )
        return resulting_revision, writeback_generation

    def _ensure_authoring_catalog_projection(
        self,
        conn: sqlite3.Connection,
        *,
        node_templates: List[Dict[str, Any]],
        handle_templates: List[Dict[str, Any]],
        authority_id: str,
        now: str,
    ) -> None:
        """在应用事务内验证候选目录，或为遗留模式持久化最小投影。

        参数说明：``conn`` 是当前唯一写事务；两个模板数组已经过服务层候选校验；
        ``authority_id`` 绑定本次编译目录指纹，``now`` 是事务时间。内存目录模式
        只核对候选与当前代际完全一致，绝不写模板表；未装配目录的遗留模式才原子
        插入最小投影。
        """

        if not isinstance(node_templates, list) or not isinstance(
            handle_templates, list
        ):
            raise StoreConflict("Candidate Catalog 投影必须是数组")
        if self._template_snapshot_provider is not None:
            self._validate_in_memory_authoring_catalog(
                node_templates=node_templates,
                handle_templates=handle_templates,
            )
            return
        for template in node_templates:
            if not isinstance(template, dict):
                raise StoreConflict("Candidate NodeTemplate 必须是对象")
            template_uuid = str(template["uuid"])
            existing = conn.execute(
                "SELECT * FROM workflow_node_template WHERE uuid = ?",
                (template_uuid,),
            ).fetchone()
            if existing is not None:
                if not self._catalog_entity_matches(
                    self._node_template_row(existing),
                    template,
                ):
                    raise StoreConflict("Candidate NodeTemplate UUID 发生语义冲突")
                conn.execute(
                    """
                    UPDATE workflow_node_template
                    SET deleted_at = NULL, update_time = ?
                    WHERE uuid = ?
                    """,
                    (now, template_uuid),
                )
                continue
            conn.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, resource_template_uuid, name,
                    display_name, class, goal, goal_default, feedback, result,
                    schema, type, icon, header, footer, node_type
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?)
                """,
                (
                    template_uuid,
                    now,
                    now,
                    template.get("description"),
                    _json(template.get("meta_data") or {}),
                    authority_id,
                    template["resource_template_uuid"],
                    template["name"],
                    template["display_name"],
                    template.get("class"),
                    _json(template.get("goal") or {}),
                    _json(template.get("goal_default") or {}),
                    _json(template.get("feedback") or {}),
                    _json(template.get("result") or {}),
                    self._catalog_schema_value(template.get("schema")),
                    template["type"],
                    template.get("icon"),
                    template.get("header"),
                    template.get("footer"),
                    template["node_type"],
                ),
            )
        for handle in handle_templates:
            if not isinstance(handle, dict):
                raise StoreConflict("Candidate HandleTemplate 必须是对象")
            handle_uuid = str(handle["uuid"])
            existing = conn.execute(
                "SELECT * FROM workflow_handle_template WHERE uuid = ?",
                (handle_uuid,),
            ).fetchone()
            if existing is not None:
                if not self._catalog_entity_matches(
                    self._handle_template_row(existing),
                    handle,
                ):
                    raise StoreConflict("Candidate HandleTemplate UUID 发生语义冲突")
                conn.execute(
                    """
                    UPDATE workflow_handle_template
                    SET deleted_at = NULL, update_time = ?
                    WHERE uuid = ?
                    """,
                    (now, handle_uuid),
                )
                continue
            conn.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, workflow_node_template_uuid,
                    handle_key, io_type, display_name, type, required,
                    data_source, data_key
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle_uuid,
                    now,
                    now,
                    handle.get("description"),
                    _json(handle.get("meta_data") or {}),
                    authority_id,
                    handle["workflow_node_template_uuid"],
                    handle["handle_key"],
                    handle["io_type"],
                    handle["display_name"],
                    handle["type"],
                    int(bool(handle["required"])),
                    handle.get("data_source"),
                    handle.get("data_key"),
                ),
            )

    def _validate_in_memory_authoring_catalog(
        self,
        *,
        node_templates: List[Dict[str, Any]],
        handle_templates: List[Dict[str, Any]],
    ) -> None:
        """证明候选模板是当前内存代际的只读子集。

        参数：两个数组来自候选工作流快照。返回：无；未知 UUID、重复实体或同一
        UUID 语义漂移时拒绝应用，且不访问 ``workflow_*_template`` SQLite 表。
        """

        snapshot = self._template_snapshot_provider.snapshot()
        candidate_node_by_uuid = self._unique_catalog_entities(
            node_templates,
            entity_name="NodeTemplate",
        )
        candidate_handle_by_uuid = self._unique_catalog_entities(
            handle_templates,
            entity_name="HandleTemplate",
        )
        expected_nodes, expected_handles = self._catalog_entities(
            candidate_node_by_uuid,
            snapshot=snapshot,
        )
        expected_node_by_uuid = {str(item["uuid"]): item for item in expected_nodes}
        expected_handle_by_uuid = {str(item["uuid"]): item for item in expected_handles}
        for template_uuid, candidate in candidate_node_by_uuid.items():
            expected = expected_node_by_uuid.get(template_uuid)
            if expected is None or not self._catalog_entity_matches(
                expected,
                candidate,
            ):
                raise StoreConflict("Candidate NodeTemplate 与当前内存目录不一致")
        if set(candidate_handle_by_uuid) != set(expected_handle_by_uuid):
            raise StoreConflict("Candidate HandleTemplate 与当前内存目录不一致")
        for handle_uuid, candidate in candidate_handle_by_uuid.items():
            if not self._catalog_entity_matches(
                expected_handle_by_uuid[handle_uuid],
                candidate,
            ):
                raise StoreConflict("Candidate HandleTemplate 与当前内存目录不一致")

    @staticmethod
    def _unique_catalog_entities(
        candidates: List[Dict[str, Any]],
        *,
        entity_name: str,
    ) -> Dict[str, Dict[str, Any]]:
        """按 UUID 索引候选目录实体并拒绝重复或非法元素。"""

        result: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise StoreConflict(f"Candidate {entity_name} 必须是对象")
            template_uuid = candidate.get("uuid")
            if not isinstance(template_uuid, str) or not template_uuid:
                raise StoreConflict(f"Candidate {entity_name} 缺少 UUID")
            if template_uuid in result:
                raise StoreConflict(f"Candidate {entity_name} UUID 重复")
            result[template_uuid] = candidate
        return result

    @staticmethod
    def _catalog_entity_matches(
        persisted: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> bool:
        """比较持久目录实体与候选投影的业务语义。

        参数说明：两个字典分别来自 SQLite 行和已校验候选；忽略投影时间，返回
        规范 JSON 是否相同，使相同 UUID 不可被静默改义。
        """

        ignored = {"create_time", "update_time", "deleted_at"}
        persisted_semantic = {
            key: value
            for key, value in persisted.items()
            if key not in ignored and value is not None
        }
        candidate_semantic = {
            key: value
            for key, value in candidate.items()
            if key not in ignored and value is not None
        }
        # 模板 schema 在 SQLite 中以文本列存储，而创作目录快照携带 JSON
        # 对象；比较前按同一 JSON 语义解码，避免仅因表示形态不同拒绝候选。
        for semantic in (persisted_semantic, candidate_semantic):
            schema = semantic.get("schema")
            if isinstance(schema, str):
                try:
                    semantic["schema"] = _load(schema, schema)
                except (TypeError, ValueError):
                    pass
        return _json(persisted_semantic) == _json(candidate_semantic)

    @staticmethod
    def _catalog_schema_value(value: Any) -> Optional[str]:
        """把候选模板 Schema 适配为当前 SQLite 文本列。

        参数说明：``value`` 可以是 ``None``、字符串或 JSON 对象；返回可写文本，
        其他类型抛出 ``StoreConflict``。F03 将负责正式目录 Schema 的版本策略。
        """

        if value is None or isinstance(value, str):
            return value
        if isinstance(value, dict):
            return _json(value)
        raise StoreConflict("Candidate NodeTemplate schema 类型无效")

    def settle_writeback(
        self,
        *,
        workflow_uuid: str,
        expected_writeback_source: str,
        expected_writeback_hash: str,
        expected_writeback_generation: str,
        observed_draft_hash: str,
        draft_update_time: str,
        event_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        with self.transaction() as conn:
            now = utc_now()
            updated = conn.execute(
                """
                UPDATE workflow_authoring
                SET observed_draft_hash = ?, draft_update_time = ?,
                    writeback_status = 'settled', writeback_source = NULL,
                    writeback_expected_hash = NULL,
                    writeback_generation = NULL, update_time = ?
                WHERE workflow_uuid = ?
                  AND writeback_status = 'pending'
                  AND writeback_source = ?
                  AND writeback_expected_hash = ?
                  AND writeback_generation = ?
                """,
                (
                    observed_draft_hash,
                    draft_update_time,
                    now,
                    workflow_uuid,
                    expected_writeback_source,
                    expected_writeback_hash,
                    expected_writeback_generation,
                ),
            )
            if updated.rowcount != 1:
                return False
            if event_data is not None:
                self._append_event(
                    conn,
                    event="workflow.authoring.changed",
                    data=event_data,
                    now=now,
                )
            return True

    def mark_writeback_pending(
        self,
        *,
        workflow_uuid: str,
        expected_writeback_source: str,
        expected_writeback_hash: str,
        expected_writeback_generation: str,
    ) -> bool:
        with self.transaction() as conn:
            updated = conn.execute(
                """
                UPDATE workflow_authoring
                SET writeback_status = 'pending', update_time = ?
                WHERE workflow_uuid = ?
                  AND writeback_source = ?
                  AND writeback_expected_hash = ?
                  AND writeback_generation = ?
                """,
                (
                    utc_now(),
                    workflow_uuid,
                    expected_writeback_source,
                    expected_writeback_hash,
                    expected_writeback_generation,
                ),
            )
            return updated.rowcount == 1

    # 事件与诊断 --------------------------------------------------------

    def latest_event_sequence(self) -> int:
        """读取当前持久事件水位；空库返回零，不读取历史载荷。"""
        with self._lock:
            return int(self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM frontend_event"
            ).fetchone()[0])

    def read_event_window(
        self, *, after_sequence: int, limit: int, event_name: str | None = None,
    ) -> tuple[int, int, List[Dict[str, Any]]]:
        """在同一 SQL 快照内读取保留边界和事件页，避免清理与续传竞态。"""
        with self._lock:
            rows = self._conn.execute(
                """
                WITH bounds AS (
                    SELECT
                        COALESCE((SELECT MIN(id) FROM frontend_event), 0) AS oldest,
                        COALESCE((SELECT MAX(id) FROM frontend_event), 0) AS latest
                ), page AS (
                    SELECT * FROM frontend_event WHERE id > ?
                        AND (CAST(? AS TEXT) IS NULL OR event = ?) ORDER BY id LIMIT ?
                )
                SELECT bounds.oldest, bounds.latest, page.*
                FROM bounds LEFT JOIN page ON 1 = 1 ORDER BY page.id
                """, (after_sequence, event_name, event_name, limit),
            ).fetchall()
        events = [
            {"id": row["id"], "event": row["event"],
             "data": _load(row["data"], {}), "create_time": row["create_time"]}
            for row in rows if row["id"] is not None
        ]
        return int(rows[0]["oldest"]), int(rows[0]["latest"]), events

    def prune_frontend_events(
        self, *, before: str, max_count: int, batch_size: int,
    ) -> int:
        """分批删除超龄或超量的连续前缀，保留最新一条作为持久水位。

        定义转发可能带来乱序时间；遇到仍需保留的事件就停止按时间清理，不能
        在序号中间挖洞。只处理通知表，不修改运行日志、任务或审计证据。
        """
        if max_count < 1 or batch_size < 1:
            raise ValueError("事件保留数量和清理批量必须为正整数")
        with self.transaction() as conn:
            latest = int(conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM frontend_event"
            ).fetchone()[0])
            floor_row = conn.execute(
                "SELECT id FROM frontend_event ORDER BY id DESC LIMIT 1 OFFSET ?",
                (max_count - 1,),
            ).fetchone()
            count_floor = int(floor_row[0]) if floor_row else 0
            rows = conn.execute(
                "SELECT id, create_time FROM frontend_event ORDER BY id LIMIT ?",
                (batch_size,),
            ).fetchall()
            through = 0
            for row in rows:
                if row["id"] >= latest:
                    break
                if row["id"] >= count_floor and row["create_time"] >= before:
                    break
                through = row["id"]
            if not through:
                return 0
            return conn.execute(
                "DELETE FROM frontend_event WHERE id <= ?", (through,),
            ).rowcount

    def list_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """读取严格晚于全局持久游标的失效通知。

        参数：``after_sequence`` 是排他事件序号，``limit`` 是物理读取上限。
        返回：按 SQLite 自增主键严格递增的事件副本。异常：查询失败时传播 SQLite
        异常；参数边界由 ``DurableEventReader`` 统一校验。本方法只读。
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM frontend_event
                WHERE id > ?
                ORDER BY id
                LIMIT ?
                """,
                (after_sequence, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event": row["event"],
                "data": _load(row["data"], {}),
                "create_time": row["create_time"],
            }
            for row in rows
        ]

    def append_forwarded_events(
        self,
        events: Iterable[Mapping[str, Any]],
    ) -> None:
        """把进程内定义目录的失效通知追加到持久全局事件流。

        参数：``events`` 是按定义目录局部序号递增的已提交事件；这里只复制事件
        类型、载荷和发生时间，并由运行事实库分配新的全局游标。返回无。异常：
        事件形状非法或 SQLite 写入失败时整批回滚，不发布部分通知。

        该投影只保存“小型失效通知”，不保存工作流定义或图；客户端收到通知后
        仍须重新读取当前进程目录，不能从事件恢复工作流权威。
        """

        normalized: list[tuple[str, Dict[str, Any], str]] = []
        for raw_event in events:
            event = str(raw_event.get("event") or "").strip()
            data = raw_event.get("data")
            create_time = str(raw_event.get("create_time") or "").strip()
            if not event or not isinstance(data, Mapping) or not create_time:
                raise ValueError("工作流定义事件形状无效")
            normalized.append((event, dict(data), create_time))
        if not normalized:
            return
        with self.transaction() as conn:
            for event, data, create_time in normalized:
                self._append_event(
                    conn,
                    event=event,
                    data=data,
                    now=create_time,
                )

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        event: str,
        data: Dict[str, Any],
        now: str,
    ) -> int:
        return append_frontend_event(
            conn,
            event=event,
            data=data,
            now=now,
        )

    @staticmethod
    def _append_runtime_event(
        conn: sqlite3.Connection,
        *,
        task_uuid: str,
        kind: str,
        now: str,
        job_uuid: Optional[str] = None,
        command_uuid: Optional[str] = None,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """在调用方事务内追加一个可重放的任务运行事实。"""

        return append_runtime_event(
            conn,
            task_uuid=task_uuid,
            kind=kind,
            now=now,
            job_uuid=job_uuid,
            command_uuid=command_uuid,
            from_status=from_status,
            to_status=to_status,
            data=data,
        )

    def count_rows(self, table: str, *, include_deleted: bool = False) -> int:
        allowed = {
            "workflow",
            "workflow_node",
            "workflow_edge",
            "workflow_task",
            "workflow_task_command",
            "workflow_node_job",
            "execution_lock_lease",
            "execution_lock_waiter",
            "workflow_inventory_requirement",
            "workflow_inventory_allocation",
            "workflow_inventory_saga",
            "workflow_authoring",
            "frontend_event",
            "workflow_runtime_journal",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table {table!r}")
        where = (
            ""
            if include_deleted
            or table
            in {
                "workflow_authoring",
                "frontend_event",
                "workflow_inventory_allocation",
                "workflow_inventory_saga",
            }
            else " WHERE deleted_at IS NULL"
        )
        with self._lock:
            # 运行事实库不再创建内存定义表；在 SQLite 和 PostgreSQL 中都把
            # 缺失的可选定义表按空结果处理，其他 SQL 错误继续抛出。
            if not self.db.table_exists(table):
                return 0
            return int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}{where}"
                ).fetchone()[0]
            )

    # 行投影 ------------------------------------------------------------

    @staticmethod
    def _base(row: sqlite3.Row) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "uuid": row["uuid"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
            "meta_data": _load(row["meta_data"], {}),
        }
        if row["description"] is not None:
            result["description"] = row["description"]
        return result

    @classmethod
    def _workflow_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """把工作流 SQLite 行恢复为包含稳定类型的定义字典。

        参数：``row`` 是已包含工作流列的 SQLite 行。返回：基础字段、名称、标签、
        ``workflow_type`` 与修订组成的新字典。异常：查询投影缺列或 JSON 损坏时
        原样暴露给仓储调用方，禁止用猜测值掩盖迁移错误。
        """

        return {
            **cls._base(row),
            "name": row["name"],
            "tags": _load(row["tags"], []),
            "workflow_type": row["workflow_type"],
            "revision": row["revision"],
        }

    @classmethod
    def _node_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_uuid": row["workflow_uuid"],
            "name": row["name"],
            "status": row["status"],
            "type": row["type"],
            "pose": _load(row["pose"], {}),
            "param": _load(row["param"], {}),
            "execution_policy": _load(row["execution_policy"], {}),
            "disabled": bool(row["disabled"]),
            "minimized": bool(row["minimized"]),
        }
        manual_confirmation = _load(row["manual_confirmation"], {})
        if manual_confirmation:
            result["manual_confirmation"] = manual_confirmation
        cls._add_optional(
            result,
            row,
            "workflow_node_template_uuid",
            "parent_uuid",
            "material_uuid",
            "icon",
            "footer",
            "action_name",
            "action_type",
            "script",
        )
        return result

    @classmethod
    def _edge_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            **cls._base(row),
            "source_node_uuid": row["source_node_uuid"],
            "target_node_uuid": row["target_node_uuid"],
            "source_handle_uuid": row["source_handle_uuid"],
            "target_handle_uuid": row["target_handle_uuid"],
        }

    @classmethod
    def _inventory_requirement_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_uuid": row["workflow_uuid"],
            "consume_node_uuid": row["consume_node_uuid"],
            "requirement_key": row["requirement_key"],
            "target_type": row["target_type"],
            "required_quantity": row["required_quantity"],
            "quantity_unit": row["quantity_unit"],
            "allow_split": bool(row["allow_split"]),
            "sort_order": row["sort_order"],
        }
        cls._add_optional(result, row, "reagent_info_uuid")
        return result

    @classmethod
    def _node_template_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "resource_template_uuid": row["resource_template_uuid"],
            "name": row["name"],
            "display_name": row["display_name"],
            "goal": _load(row["goal"], {}),
            "goal_default": _load(row["goal_default"], {}),
            "feedback": _load(row["feedback"], {}),
            "result": _load(row["result"], {}),
            "type": row["type"],
            "node_type": row["node_type"],
        }
        cls._add_optional(
            result,
            row,
            "class",
            "schema",
            "icon",
            "header",
            "footer",
        )
        return result

    @classmethod
    def _handle_template_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_node_template_uuid": row["workflow_node_template_uuid"],
            "handle_key": row["handle_key"],
            "io_type": row["io_type"],
            "display_name": row["display_name"],
            "type": row["type"],
            "required": bool(row["required"]),
        }
        cls._add_optional(result, row, "data_source", "data_key")
        return result

    @staticmethod
    def _task_control_values(row: sqlite3.Row) -> tuple[str, str]:
        """兼容解码旧 Task 的单步执行状态与独立派发闸门。"""

        execution_mode = str(row["execution_mode"] or "normal")
        # ``run_mode`` 是创建时冻结的模式。单步任务一旦恢复为自动运行，
        # ``execution_mode`` 会写成 normal；之后的暂停不能再把它读回 step。
        dispatch_gate = str(row["dispatch_gate"] or "active")
        if execution_mode in {"switching_to_step", "step"}:
            dispatch_gate = "paused"
        return execution_mode, dispatch_gate

    @classmethod
    def _task_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """把工作流任务（WorkflowTask）数据库行恢复为公共领域投影。

        参数：``row`` 是同一工作流写模型中的 SQLite 行。返回包含执行来源
        ``execution_kind`` 的字典；内部幂等键与请求指纹不对外暴露。
        """

        execution_mode, dispatch_gate = cls._task_control_values(row)
        result = {
            **cls._base(row),
            "workflow_uuid": row["workflow_uuid"],
            "execution_kind": row["execution_kind"],
            "priority": _stored_task_priority(row["priority"]),
            "status": row["status"],
            "workflow_snapshot": _load(row["workflow_snapshot"], {}),
            "execution_plan": _load(row["execution_plan"], {}),
            "run_mode": row["run_mode"],
            "execution_mode": execution_mode,
            "dispatch_gate": dispatch_gate,
            "control_status": row["control_status"],
            "cleanup_status": row["cleanup_status"],
            "wait_reason": _load(row["wait_reason"], {}),
            "trace_context": _load(row["trace_context"], {}),
            "input": _load(row["input"], {}),
            "output": _load(row["output"], {}),
            "error_info": _load(row["error_info"], []),
        }
        cls._add_optional(
            result,
            row,
            "target_node_uuid",
            "timeout_at",
            "attention_reason",
            "terminal_ghost_detected_at",
            "reconciliation_resume_control_status",
            "started_at",
            "finished_at",
            "backend_task_uuid",
            "invocation_key",
            "revision_fingerprint",
        )
        if row["backend_task_uuid"] is not None:
            result["global_task_uuid"] = row["backend_task_uuid"]
        if row["timeout_at"] is not None:
            result["deadline"] = row["timeout_at"]
        if "definition_version" in row.keys():
            result["definition_version"] = str(row["definition_version"])
        return result

    @classmethod
    def _task_presentation_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """解码已经由 SQLite 裁剪的 Edge Task 展示行。"""

        execution_mode, dispatch_gate = cls._task_control_values(row)
        result = {
            "uuid": row["uuid"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
            "meta_data": _load(row["meta_data"], {}),
            "workflow_uuid": row["workflow_uuid"],
            "execution_kind": row["execution_kind"],
            "priority": _stored_task_priority(row["priority"]),
            "status": row["status"],
            "workflow_snapshot": _load(row["workflow_snapshot"], {}),
            "execution_plan": _load(row["execution_plan"], {}),
            "run_mode": row["run_mode"],
            "execution_mode": execution_mode,
            "dispatch_gate": dispatch_gate,
            "control_status": row["control_status"],
            "cleanup_status": row["cleanup_status"],
            "wait_reason": _load(row["wait_reason"], {}),
            "trace_context": _load(row["trace_context"], {}),
            "input": _load(row["input"], {}),
            "material_uuids": _task_input_material_uuids(
                _load(row["input_contract_parameters"], []),
                _load(row["task_input_source"], {}),
            ),
            "error_info": _load(row["error_info"], []),
        }
        cls._add_optional(
            result,
            row,
            "description",
            "target_node_uuid",
            "attention_reason",
            "started_at",
            "finished_at",
        )
        return result

    @classmethod
    def _task_command_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_task_uuid": row["workflow_task_uuid"],
            "type": row["type"],
            "idempotency_key": row["idempotency_key"],
            "status": row["status"],
            "result": _load(row["result"], {}),
            "trace_context": _load(row["trace_context"], {}),
        }
        cls._add_optional(result, row, "target_node_uuid", "consumed_at")
        return result

    @classmethod
    def _job_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_uuid": row["workflow_node_uuid"],
            "feedback_sequence": row["feedback_sequence"],
            "topological_index": row["topological_index"],
            "executor_kind": row["executor_kind"],
            "execution_policy": _load(row["execution_policy"], {}),
            "execution_timeout_seconds": row["execution_timeout_seconds"],
            "status": row["status"],
            "attempt": row["attempt"],
            "param": _load(row["param"], {}),
            "current_attempt": bool(row["current_attempt"]),
            "start_state": row["start_state"],
            "execution_source": row["execution_source"],
            "retry_of_job_uuid": row["retry_of_job_uuid"],
            "definition_fingerprint": row["definition_fingerprint"],
            "feedback_data": _load(row["feedback_data"], {}),
            "return_info": _load(row["return_info"], {}),
            "control_data": _load(row["control_data"], {}),
            "error_info": _load(row["error_info"], []),
            "wait_reason": _load(row["wait_reason"], {}),
            "expected_change_set": _load(row["expected_change_set"], {}),
        }
        cls._add_optional(
            result,
            row,
            "material_uuid",
            ("edge_agent_uuid", "edge_uuid"),
            "edge_command_uuid",
            "dispatch_deadline_at",
            "execution_deadline_at",
            "cancel_command_uuid",
            "cancel_ack_deadline_at",
            "cancel_complete_deadline_at",
            "cancel_accepted_at",
            "uncertainty_reason",
            "dispatch_effect_uuid",
            "dispatch_parameter_hash",
            "started_at",
            "finished_at",
        )
        result.update(manual_review_projection(result))
        return result

    @classmethod
    def _job_presentation_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """解码已经由 SQL 排除执行策略与派发凭据的 Job 展示行。"""

        result = {
            "uuid": row["uuid"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_uuid": row["workflow_node_uuid"],
            "topological_index": row["topological_index"],
            "executor_kind": row["executor_kind"],
            "status": row["status"],
            "attempt": row["attempt"],
            "current_attempt": bool(row["current_attempt"]),
            "retry_of_job_uuid": row["retry_of_job_uuid"],
            "execution_source": row["execution_source"],
            "start_state": row["start_state"],
            "control_data": _load(row["control_data"], {}),
            "error_info": _load(row["error_info"], []),
            "wait_reason": _load(row["wait_reason"], {}),
            "expected_change_set": _load(row["expected_change_set"], {}),
        }
        cls._add_optional(
            result,
            row,
            "material_uuid",
            "uncertainty_reason",
            "started_at",
            "finished_at",
        )
        if result["execution_source"] == "error_manual":
            result.update(manual_review_projection(_load(row["manual_review_basis"], {})))
        return result

    @staticmethod
    def _runtime_event_row(row: sqlite3.Row) -> Dict[str, Any]:
        """把运行日志查询行投影成前端稳定合同。"""

        result: Dict[str, Any] = {
            "sequence": row["event_sequence"],
            "workflow_task_uuid": row["event_task_uuid"],
            "kind": row["event_kind"],
            "data": _load(row["event_data"], {}),
            "create_time": row["event_create_time"],
        }
        for column, output in (
            ("event_job_uuid", "workflow_node_job_uuid"),
            ("event_command_uuid", "workflow_task_command_uuid"),
            ("event_from_status", "from_status"),
            ("event_to_status", "to_status"),
            ("job_workflow_node_uuid", "workflow_node_uuid"),
            ("job_executor_kind", "executor_kind"),
            ("job_attempt", "attempt"),
        ):
            value = row[column]
            if value is not None:
                result[output] = value

        kind = row["event_kind"]
        to_status = row["event_to_status"]
        if kind == "job_transition" and to_status == "dispatched":
            result["param"] = _load(row["job_param"], {})
        if kind == "job_transition" and to_status in {
            "succeeded",
            "failed",
            "skipped",
            "canceled",
            "timeout",
        }:
            result["return_info"] = _load(row["job_return_info"], {})
            result["error_info"] = _load(row["job_error_info"], [])
        return result

    @staticmethod
    def _add_optional(
        result: Dict[str, Any],
        row: sqlite3.Row,
        *fields: str | Tuple[str, str],
    ) -> None:
        for field in fields:
            column, output = field if isinstance(field, tuple) else (field, field)
            value = row[column]
            if value is not None:
                result[output] = value


__all__ = [
    "StoreAuthoringConflict",
    "StoreConflict",
    "StoreNotFound",
    "StoreRevisionConflict",
    "WorkflowStore",
    "utc_now",
]
