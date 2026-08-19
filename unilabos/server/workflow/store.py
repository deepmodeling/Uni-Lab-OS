"""Backend-shaped Workflow 与 Authoring 事实的 SQLite Authority。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from uuid import uuid4

from unilabos.server.workflow.graph_validation import (
    GraphValidationError,
    MissingTemplateError,
    validate_graph,
)
from unilabos.server.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.server.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.server.workflow.schema import (
    WORKFLOW_SCHEMA_VERSION,
    WorkflowSchemaError,
    migrate_workflow_schema,
)

_STORE_INITIALIZATION_BUSY_TIMEOUT_SECONDS = 5.0
_STORE_INITIALIZATION_SQLITE_BUSY_TIMEOUT_MS = 100
_STORE_INITIALIZATION_RETRY_INTERVAL_SECONDS = 0.01
_STORE_SQLITE_BUSY_TIMEOUT_MS = 5000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: Optional[str], fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    return decode_json_bytes(value.encode("utf-8"))


class StoreNotFound(LookupError):
    pass


class StoreConflict(RuntimeError):
    pass


class StoreRevisionConflict(StoreConflict):
    pass


class StoreAuthoringConflict(StoreConflict):
    """Apply 事务提交前发生了 Authoring 前置条件冲突。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
);
CREATE INDEX IF NOT EXISTS ix_workflow_task_workflow
    ON workflow_task(workflow_uuid);
CREATE INDEX IF NOT EXISTS ix_workflow_task_status
    ON workflow_task(status);

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
"""


class WorkflowStore:
    """由单一连接持有的 SQLite Workflow Authority。

    Store 方法用一个进程内可重入锁串行化事务。Workflow 专属编排锁由
    ``WorkflowService`` 持有，使文件与数据库操作共享同一临界区。
    """

    def __init__(self, db_path: str | Path):
        initialization_deadline = (
            monotonic() + _STORE_INITIALIZATION_BUSY_TIMEOUT_SECONDS
        )
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
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
                current_schema_version = int(
                    self._conn.execute("PRAGMA user_version").fetchone()[0]
                )
                if current_schema_version > WORKFLOW_SCHEMA_VERSION:
                    raise WorkflowSchemaError(
                        "workflow database schema version "
                        f"{current_schema_version} is newer than supported "
                        f"{WORKFLOW_SCHEMA_VERSION}"
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
                    migrate_workflow_schema(self._conn)
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
                        """
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
    ) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO workflow(
                        uuid, create_time, update_time, deleted_at,
                        description, meta_data, name, tags, revision
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 1)
                    """,
                    (
                        workflow_uuid,
                        now,
                        now,
                        description,
                        _json(meta_data),
                        name,
                        _json(tags),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"workflow {workflow_uuid} already exists") from exc
        return self.get_workflow(workflow_uuid)

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
    ) -> Dict[str, Any]:
        where = "deleted_at IS NULL"
        values: List[Any] = []
        if name:
            where += " AND name LIKE ?"
            values.append(f"%{name}%")
        offset = (page - 1) * page_size
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM workflow WHERE {where}",
                values,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow WHERE {where}
                ORDER BY create_time DESC, uuid
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
    ) -> Dict[str, Any]:
        with self.transaction() as conn:
            self.get_workflow(workflow_uuid, conn=conn)
            conn.execute(
                """
                UPDATE workflow
                SET name = ?, tags = ?, description = ?, meta_data = ?,
                    update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (
                    name,
                    _json(tags),
                    description,
                    _json(meta_data),
                    utc_now(),
                    workflow_uuid,
                ),
            )
        return self.get_workflow(workflow_uuid)

    def delete_workflow(self, workflow_uuid: str) -> None:
        now = utc_now()
        with self.transaction() as conn:
            self.get_workflow(workflow_uuid, conn=conn)
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
            template_uuids = [
                row["workflow_node_template_uuid"]
                for row in node_rows
                if row["workflow_node_template_uuid"]
            ]
            node_templates: List[Dict[str, Any]] = []
            handle_templates: List[Dict[str, Any]] = []
            if template_uuids:
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
                node_templates = [self._node_template_row(row) for row in template_rows]
                handle_templates = [
                    self._handle_template_row(row) for row in handle_rows
                ]
        return {
            "workflow": workflow,
            "nodes": [self._node_row(row) for row in node_rows],
            "edges": [self._edge_row(row) for row in edge_rows],
            "node_templates": node_templates,
            "handle_templates": handle_templates,
        }

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        protect_reserved_metadata: bool = False,
    ) -> Dict[str, Any]:
        with self.transaction() as conn:
            self._reconcile_graph(
                conn,
                workflow_uuid=workflow_uuid,
                expected_revision=revision,
                nodes=nodes,
                edges=edges,
                advance_revision=True,
                protect_reserved_metadata=protect_reserved_metadata,
            )
        return self.get_graph(workflow_uuid)

    def _reconcile_graph(
        self,
        conn: sqlite3.Connection,
        *,
        workflow_uuid: str,
        expected_revision: int,
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        advance_revision: bool,
        protect_reserved_metadata: bool = False,
        semantic_workflow_meta_data: Optional[Dict[str, Any]] = None,
    ) -> int:
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
        template_uuids = sorted(
            {
                node.workflow_node_template_uuid
                for node in nodes
                if node.workflow_node_template_uuid is not None
            }
        )
        templates: Dict[str, Dict[str, Any]] = {}
        handles: Dict[str, Dict[str, Any]] = {}
        if template_uuids:
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
            node.uuid: self._graph_node_param(conn, node) for node in nodes
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
            )
        except MissingTemplateError as exc:
            raise StoreNotFound(str(exc)) from exc
        except GraphValidationError as exc:
            raise StoreConflict(str(exc)) from exc
        now = utc_now()
        for node in nodes:
            self._upsert_node(
                conn,
                workflow_uuid,
                node,
                now,
                protect_reserved_metadata=protect_reserved_metadata,
            )
        for edge in edges:
            self._upsert_edge(
                conn,
                workflow_uuid,
                edge,
                now,
                protect_reserved_metadata=protect_reserved_metadata,
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

    def _upsert_node(
        self,
        conn: sqlite3.Connection,
        workflow_uuid: str,
        node: WorkflowNodeWrite,
        now: str,
        *,
        protect_reserved_metadata: bool,
    ) -> None:
        existing = conn.execute(
            "SELECT workflow_uuid, create_time, meta_data "
            "FROM workflow_node WHERE uuid = ?",
            (node.uuid,),
        ).fetchone()
        if existing is not None and existing["workflow_uuid"] != workflow_uuid:
            raise StoreConflict(
                f"workflow node {node.uuid} belongs to another workflow"
            )
        meta_data = self._protected_metadata(
            node.meta_data,
            existing["meta_data"] if existing is not None else None,
            enabled=protect_reserved_metadata,
        )
        values = (
            node.description,
            _json(meta_data),
            workflow_uuid,
            node.workflow_node_template_uuid,
            node.parent_uuid,
            node.material_uuid,
            node.name,
            node.status,
            node.type,
            node.icon,
            _json(node.pose),
            _json(self._graph_node_param(conn, node)),
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
                    param, footer, action_name, action_type, execution_policy,
                    disabled, minimized, script
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?)
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
                pose = ?, param = ?, footer = ?, action_name = ?,
                action_type = ?, execution_policy = ?, disabled = ?,
                minimized = ?, script = ?
            WHERE uuid = ?
            """,
            (now, *values, node.uuid),
        )

    @staticmethod
    def _graph_node_param(
        conn: sqlite3.Connection,
        node: WorkflowNodeWrite,
    ) -> Dict[str, Any]:
        if node.param is not None:
            return node.param
        if node.workflow_node_template_uuid is None:
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
            raise StoreConflict(
                f"workflow edge {edge.uuid} belongs to another workflow"
            )
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
        result = dict(submitted)
        if not enabled:
            return result
        result.pop("unilab", None)
        existing = _load(existing_json, {}) if existing_json is not None else {}
        if isinstance(existing, dict) and "unilab" in existing:
            result["unilab"] = existing["unilab"]
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

    def create_task_with_jobs(
        self,
        *,
        workflow_uuid: str,
        task_uuid: str,
        run_mode: str,
        target_node_uuid: Optional[str],
        input_value: Dict[str, Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        plan_builder: Callable[
            [Dict[str, Any]], Tuple[Dict[str, Any], List[Dict[str, Any]]]
        ],
    ) -> Dict[str, Any]:
        now = utc_now()
        with self.transaction() as conn:
            graph = self.get_graph(workflow_uuid, conn=conn)
            plan, jobs = plan_builder(graph)
            effective_run_mode = str(plan["run_mode"])
            effective_target = plan.get("target_node_uuid")
            control_status = "paused" if effective_run_mode == "step" else "active"
            conn.execute(
                """
                INSERT INTO workflow_task(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, status, workflow_snapshot,
                    execution_plan, run_mode, target_node_uuid, control_status,
                    cleanup_status, trace_context, input, output, error_info
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'pending', ?, ?, ?, ?, ?,
                          'none', '{}', ?, '{}', '[]')
                """,
                (
                    task_uuid,
                    now,
                    now,
                    description,
                    _json(meta_data),
                    workflow_uuid,
                    _json(graph),
                    _json(plan),
                    effective_run_mode,
                    effective_target,
                    control_status,
                    _json(input_value),
                ),
            )
            for job in jobs:
                conn.execute(
                    """
                    INSERT INTO workflow_node_job(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, workflow_task_uuid, workflow_node_uuid,
                        material_uuid, feedback_sequence, topological_index,
                        executor_kind, execution_policy,
                        execution_timeout_seconds, status, attempt, param,
                        feedback_data, return_info, control_data, error_info
                    ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, 0, ?, ?, ?,
                              ?, 'pending', 1, ?, '{}', '{}', '{}', '[]')
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
                        _json(job.get("param") or {}),
                    ),
                )
        return self.get_task(task_uuid)

    def get_task(self, task_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow task {task_uuid} not found")
        return self._task_row(row)

    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        workflow_uuid: Optional[str] = None,
        status: str = "",
        cleanup_status: str = "",
    ) -> Dict[str, Any]:
        clauses = ["deleted_at IS NULL"]
        values: List[Any] = []
        for field, value in (
            ("workflow_uuid", workflow_uuid),
            ("status", status),
            ("cleanup_status", cleanup_status),
        ):
            if value:
                clauses.append(f"{field} = ?")
                values.append(value)
        where = " AND ".join(clauses)
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

    @staticmethod
    def _runtime_limit_clause(limit: Optional[int], offset: int) -> tuple[str, tuple[int, ...]]:
        if limit is None:
            return "LIMIT -1 OFFSET ?", (offset,)
        return "LIMIT ? OFFSET ?", (limit, offset)

    def list_manual_confirmations(
        self, task_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        self.get_task(task_uuid)
        clause, paging = self._runtime_limit_clause(limit, offset)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_manual_confirmation
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                ORDER BY create_time, uuid
                {clause}
                """,
                (task_uuid, *paging),
            ).fetchall()
        return [self._manual_confirmation_row(row) for row in rows]

    def list_interventions(
        self, task_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        self.get_task(task_uuid)
        clause, paging = self._runtime_limit_clause(limit, offset)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_intervention
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                ORDER BY opened_at, uuid
                {clause}
                """,
                (task_uuid, *paging),
            ).fetchall()
        return [self._intervention_row(row) for row in rows]

    def list_job_results(
        self, job_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        self.get_job(job_uuid)
        clause, paging = self._runtime_limit_clause(limit, offset)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_node_job_result
                WHERE workflow_node_job_uuid = ? AND deleted_at IS NULL
                ORDER BY committed_at, uuid
                {clause}
                """,
                (job_uuid, *paging),
            ).fetchall()
        return [self._job_result_row(row) for row in rows]

    def list_job_feedback_history(
        self, job_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        self.get_job(job_uuid)
        clause, paging = self._runtime_limit_clause(limit, offset)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_node_job_feedback_history
                WHERE workflow_node_job_uuid = ? AND deleted_at IS NULL
                ORDER BY sequence, uuid
                {clause}
                """,
                (job_uuid, *paging),
            ).fetchall()
        return [self._job_feedback_row(row) for row in rows]

    def list_recoverable_tasks(self) -> List[Dict[str, Any]]:
        """返回规范本地执行器可恢复的任务，不改写任何状态。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM workflow_task
                WHERE deleted_at IS NULL
                  AND execution_kind = 'workflow'
                  AND status IN ('pending', 'running', 'canceling')
                ORDER BY create_time, uuid
                """
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def prepare_task_execution(self, task_uuid: str) -> Dict[str, Any]:
        """认领/恢复任务，并以 job 终态作为持久 completed cursor。

        已成功 job 不会重跑。崩溃时处于 dispatched/running 的 job 无法证明设备
        是否已经产生副作用，必须转 execution_unknown 等待对账，禁止盲目重放。
        """

        now = utc_now()
        with self.transaction() as conn:
            task_row = conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if task_row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            task = self._task_row(task_row)
            jobs_rows = conn.execute(
                "SELECT * FROM workflow_node_job WHERE workflow_task_uuid = ? "
                "AND deleted_at IS NULL ORDER BY topological_index, create_time, uuid",
                (task_uuid,),
            ).fetchall()
            jobs = [self._job_row(row) for row in jobs_rows]
            if task["status"] in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                return {"state": "terminal", "task": task, "jobs": jobs}
            if task["control_status"] != "active":
                return {"state": task["control_status"], "task": task, "jobs": jobs}

            statuses = {job["status"] for job in jobs}
            uncertain = {
                "dispatched",
                "running",
                "intervention_required",
                "cancel_requested",
                "execution_unknown",
            }
            if statuses & uncertain:
                reason = "process restarted with an in-flight workflow node job"
                conn.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = CASE
                            WHEN status IN ('dispatched', 'running', 'cancel_requested')
                            THEN 'execution_unknown'
                            ELSE status
                        END,
                        uncertainty_reason = CASE
                            WHEN status IN ('dispatched', 'running', 'cancel_requested')
                            THEN ? ELSE uncertainty_reason END,
                        update_time = ?
                    WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                      AND status IN (
                          'dispatched', 'running', 'cancel_requested',
                          'execution_unknown', 'intervention_required'
                      )
                    """,
                    (reason, now, task_uuid),
                )
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = 'waiting_reconciliation',
                        attention_reason = ?, update_time = ?
                    WHERE uuid = ?
                    """,
                    (reason, now, task_uuid),
                )
                self._append_event(
                    conn,
                    event="workflow.task.changed",
                    data={
                        "workflow_task_uuid": task_uuid,
                        "status": task["status"],
                        "control_status": "waiting_reconciliation",
                    },
                    now=now,
                )
                state = "waiting_reconciliation"
            elif not jobs or statuses <= {"succeeded", "skipped"}:
                conn.execute(
                    "UPDATE workflow_task SET status='succeeded', output='{}', "
                    "finished_at=?, update_time=? WHERE uuid=?",
                    (now, now, task_uuid),
                )
                state = "terminal"
            elif statuses & {"failed", "timeout", "canceled"}:
                status = (
                    "failed"
                    if "failed" in statuses
                    else ("timeout" if "timeout" in statuses else "canceled")
                )
                conn.execute(
                    "UPDATE workflow_task SET status=?, finished_at=?, update_time=? "
                    "WHERE uuid=?",
                    (status, now, now, task_uuid),
                )
                state = "terminal"
            else:
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET status = 'running', started_at = COALESCE(started_at, ?),
                        update_time = ?
                    WHERE uuid = ?
                    """,
                    (now, now, task_uuid),
                )
                state = "ready"
        return {
            "state": state,
            "task": self.get_task(task_uuid),
            "jobs": self.list_jobs(task_uuid),
        }

    def mark_job_running(self, job_uuid: str) -> Dict[str, Any]:
        """在发送设备副作用前持久化 job running。"""

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if row["status"] in {
                "succeeded", "failed", "skipped", "canceled", "timeout"
            }:
                return self._job_row(row)
            if row["status"] not in {"pending", "dispatched", "running"}:
                raise StoreConflict(
                    f"workflow node job {job_uuid} cannot run from {row['status']}"
                )
            conn.execute(
                """
                UPDATE workflow_node_job
                SET status='running', started_at=COALESCE(started_at, ?),
                    update_time=? WHERE uuid=?
                """,
                (now, now, job_uuid),
            )
        return self.get_job(job_uuid)

    def record_job_terminal(
        self,
        job_uuid: str,
        *,
        status: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """先持久化节点终态，再允许 DAG 释放后继依赖。"""

        if status not in {"succeeded", "failed", "skipped", "canceled", "timeout"}:
            raise ValueError(f"invalid terminal workflow node job status: {status}")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if row["status"] in {
                "succeeded", "failed", "skipped", "canceled", "timeout"
            }:
                return self._job_row(row)
            conn.execute(
                """
                UPDATE workflow_node_job
                SET status=?, return_info=?, error_info=?, finished_at=?, update_time=?
                WHERE uuid=?
                """,
                (
                    status,
                    _json(return_info),
                    _json(error_info),
                    now,
                    now,
                    job_uuid,
                ),
            )
            self._append_event(
                conn,
                event="workflow.node_job.changed",
                data={"workflow_node_job_uuid": job_uuid, "status": status},
                now=now,
            )
        return self.get_job(job_uuid)

    def finish_task(
        self,
        task_uuid: str,
        *,
        status: str,
        output: Dict[str, Any],
        error_info: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if status not in {"succeeded", "failed", "canceled", "timeout"}:
            raise ValueError(f"invalid terminal workflow task status: {status}")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM workflow_task WHERE uuid=? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if row["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                return self.get_task(task_uuid)
            conn.execute(
                """
                UPDATE workflow_task
                SET status=?, output=?, error_info=?, finished_at=?, update_time=?
                WHERE uuid=?
                """,
                (status, _json(output), _json(error_info), now, now, task_uuid),
            )
            self._append_event(
                conn,
                event="workflow.task.changed",
                data={"workflow_task_uuid": task_uuid, "status": status},
                now=now,
            )
        return self.get_task(task_uuid)

    # Authoring ----------------------------------------------------------

    def register_source(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str,
        relative_path: str,
        source_uri: str,
    ) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self.transaction() as conn:
                self.get_workflow(workflow_uuid, conn=conn)
                conn.execute(
                    """
                    INSERT INTO workflow_source_registration(
                        workflow_uuid, package_id, package_root, relative_path,
                        source_uri, create_time, update_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_uuid) DO UPDATE SET
                        package_id = excluded.package_id,
                        package_root = excluded.package_root,
                        relative_path = excluded.relative_path,
                        source_uri = excluded.source_uri,
                        update_time = excluded.update_time
                    """,
                    (
                        workflow_uuid,
                        package_id,
                        package_root,
                        relative_path,
                        source_uri,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO workflow_authoring(
                        workflow_uuid, diagnostics, update_time
                    ) VALUES (?, '[]', ?)
                    ON CONFLICT(workflow_uuid) DO NOTHING
                    """,
                    (workflow_uuid, now),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreConflict("工作流源码身份已被占用") from exc
        return self.get_source_registration(workflow_uuid)

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
        expected_revision: int,
        expected_draft_hash: str,
        expected_candidate_hash: str,
        expected_catalog_fingerprint: str,
        candidate: Dict[str, Any],
        applied_source: Dict[str, Any],
        event_data: Dict[str, Any],
    ) -> Tuple[int, str]:
        changeset = candidate["changeset"]
        kind = changeset["kind"]
        graph = candidate["graph"]
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
            if (
                authoring is None
                or authoring["observed_draft_hash"] != expected_draft_hash
            ):
                raise StoreAuthoringConflict("draft_hash_conflict")
            workflow = self.get_workflow(workflow_uuid, conn=conn)
            if workflow["revision"] != expected_revision:
                raise StoreRevisionConflict("workflow revision changed before apply")
            stored_candidate = _load(authoring["candidate"], None)
            if not isinstance(stored_candidate, dict):
                raise StoreAuthoringConflict("candidate_not_ready")
            if (
                stored_candidate.get("template_catalog_fingerprint")
                != expected_catalog_fingerprint
            ):
                raise StoreAuthoringConflict("template_catalog_conflict")
            if authoring["candidate_hash"] != expected_candidate_hash:
                raise StoreAuthoringConflict("candidate_hash_conflict")
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
                resulting_revision = self._reconcile_graph(
                    conn,
                    workflow_uuid=workflow_uuid,
                    expected_revision=expected_revision,
                    nodes=nodes,
                    edges=edges,
                    advance_revision=True,
                    protect_reserved_metadata=False,
                    semantic_workflow_meta_data=candidate_meta,
                )
                workflow_meta = dict(workflow["meta_data"])
                workflow_meta.pop("unilab", None)
                if "unilab" in candidate_meta:
                    if candidate_meta["unilab"] is not None:
                        workflow_meta["unilab"] = candidate_meta["unilab"]
                conn.execute(
                    """
                    UPDATE workflow
                    SET meta_data = ?, update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (_json(workflow_meta), now, workflow_uuid),
                )
            elif kind == "source_only":
                resulting_revision = expected_revision
            else:
                raise StoreConflict(f"unsupported Authoring changeset kind {kind!r}")
            applied_source = {
                **applied_source,
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
                    **event_data,
                    "workflow_revision": resulting_revision,
                },
                now=now,
            )
            return resulting_revision, writeback_generation

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

    def list_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM frontend_event
                WHERE sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (after_id, limit),
            ).fetchall()
        return [
            {
                "id": row["sequence"],
                "uuid": row["uuid"],
                "event": row["type"],
                "aggregate_uuid": row["aggregate_uuid"],
                "data": _load(row["payload"], {}),
                "create_time": row["create_time"],
            }
            for row in rows
        ]

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        event: str,
        data: Dict[str, Any],
        now: str,
    ) -> int:
        aggregate_uuid = next(
            (
                str(data[key])
                for key in ("workflow_uuid", "task_uuid", "uuid")
                if data.get(key)
            ),
            "00000000-0000-0000-0000-000000000000",
        )
        cursor = conn.execute(
            """
            INSERT INTO frontend_event(
                uuid, create_time, type, aggregate_uuid, payload
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid4()), now, event, aggregate_uuid, _json(data)),
        )
        return int(cursor.lastrowid)

    def count_rows(self, table: str, *, include_deleted: bool = False) -> int:
        allowed = {
            "workflow",
            "workflow_node",
            "workflow_edge",
            "workflow_task",
            "workflow_node_job",
            "workflow_task_command",
            "workflow_node_job_feedback_history",
            "workflow_node_job_result",
            "workflow_intervention",
            "workflow_manual_confirmation",
            "execution_lock_lease",
            "workflow_authoring",
            "frontend_event",
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
            }
            else " WHERE deleted_at IS NULL"
        )
        with self._lock:
            return int(
                self._conn.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
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
        return {
            **cls._base(row),
            "name": row["name"],
            "tags": _load(row["tags"], []),
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

    @classmethod
    def _task_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_uuid": row["workflow_uuid"],
            "execution_kind": row["execution_kind"],
            "status": row["status"],
            "workflow_snapshot": _load(row["workflow_snapshot"], {}),
            "execution_plan": _load(row["execution_plan"], {}),
            "run_mode": row["run_mode"],
            "control_status": row["control_status"],
            "cleanup_status": row["cleanup_status"],
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
        )
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
            "feedback_data": _load(row["feedback_data"], {}),
            "return_info": _load(row["return_info"], {}),
            "control_data": _load(row["control_data"], {}),
            "error_info": _load(row["error_info"], []),
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
            "uncertainty_reason",
            "started_at",
            "finished_at",
        )
        return result

    @classmethod
    def _manual_confirmation_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            "status": row["status"],
            "assignee_user_ids": _load(row["assignee_user_ids"], []),
            "param": _load(row["param"], {}),
            "opened_at": row["opened_at"],
        }
        cls._add_optional(
            result,
            row,
            "confirmed_by",
            "comment",
            "decision_idempotency_key",
            "deadline_at",
            "decided_at",
        )
        return result

    @classmethod
    def _intervention_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            "edge_agent_uuid": row["edge_agent_uuid"],
            "revision": row["revision"],
            "status": row["status"],
            "options": _load(row["options"], []),
            "resume_control_status": row["resume_control_status"],
            "selected_option": _load(row["selected_option"], {}),
            "opened_at": row["opened_at"],
        }
        cls._add_optional(
            result,
            row,
            "selected_option_id",
            "decision_idempotency_key",
            "edge_command_uuid",
            "decided_at",
        )
        return result

    @classmethod
    def _job_result_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            "edge_command_uuid": row["edge_command_uuid"],
            "idempotency_key": row["idempotency_key"],
            "outcome": row["outcome"],
            "return_info": _load(row["return_info"], {}),
            "error_info": _load(row["error_info"], []),
            "committed_at": row["committed_at"],
        }
        cls._add_optional(result, row, "consumed_at")
        return result

    @classmethod
    def _job_feedback_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            "sequence": row["sequence"],
            "feedback_type": row["feedback_type"],
            "data": _load(row["data"], {}),
            "observed_at": row["observed_at"],
            "received_at": row["received_at"],
            "idempotency_key": row["idempotency_key"],
        }
        cls._add_optional(result, row, "published_at")
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
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowStore",
    "utc_now",
]
