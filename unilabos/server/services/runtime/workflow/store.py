"""Backend-shaped Workflow 与 Authoring 事实的 SQLite Authority。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from uuid import uuid4

from unilabos.protocol.utils.json_codec import decode_json_bytes, encode_json, strict_json_equal
from unilabos.protocol.runtime.workflow import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.protocol.utils.workflow_validation import (
    GraphValidationError,
    MissingTemplateError,
    validate_graph,
)
from unilabos.server.services.runtime.workflow.errors import (
    StoreAuthoringConflict,
    StoreConflict,
    StoreNotFound,
    StoreRevisionConflict,
)
from unilabos.server.database.connection import SerializedConnection
from unilabos.server.database.schema import initialize_database
from unilabos.server.database.sqlite_domain import SqliteDomain
from unilabos.server.database.tables.runtime import RUNTIME_DATABASE

_STORE_SQLITE_BUSY_TIMEOUT_MS = 5000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: Optional[str], fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    return decode_json_bytes(value.encode("utf-8"))


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    """解析本地生成的 UTC ISO 时间；坏值按无期限处理，避免读接口崩溃。"""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class WorkflowStore:
    """SQLite Workflow Authority（表落 ``runtime.db``）。

    ``WorkflowService`` 直接继承本类：持连接、写事务与业务 API 在同一个
    实例上。Store 方法用一个进程内可重入锁串行化事务。

    生产组合根把 RuntimeService 实例作为 ``database`` 传入，共享其
    connection 与 write_lock（同库单连接单写者）；测试可传路径或
    ``:memory:`` 独立建库。
    """

    def __init__(
        self,
        database: "str | Path | sqlite3.Connection | SerializedConnection | SqliteDomain | WorkflowStore",
        *,
        lock: Optional[threading.RLock] = None,
    ):
        if isinstance(database, (SqliteDomain, WorkflowStore)):
            # 同库共存域：共享宿主的连接与写锁
            if lock is None:
                lock = database.write_lock
            database = database.connection
        if isinstance(database, SerializedConnection):
            if lock is not None and lock is not database.write_lock:
                raise ValueError("共享 SQLite 连接必须使用同一把读写锁")
            lock = database.write_lock
        self._lock = lock if lock is not None else threading.RLock()
        if isinstance(database, (sqlite3.Connection, SerializedConnection)):
            self.path = ""
            self._conn = database
            self._owns_connection = False
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        else:
            self.path = str(database)
            self._owns_connection = True
            if self.path == ":memory:":
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA foreign_keys = ON")
                with self._conn:
                    for statement in RUNTIME_DATABASE.statements():
                        self._conn.execute(statement)
            else:
                self._conn = initialize_database(Path(self.path), RUNTIME_DATABASE)
            self._conn.execute(
                f"PRAGMA busy_timeout = {_STORE_SQLITE_BUSY_TIMEOUT_MS}"
            )
        if not isinstance(self._conn, SerializedConnection):
            self._conn = SerializedConnection(self._conn, self._lock)

    @property
    def connection(self) -> SerializedConnection:
        """底层 SQLite 连接（与其他域 Service 的属性面一致）。"""

        return self._conn

    @property
    def write_lock(self) -> threading.RLock:
        """runtime.db 的进程内唯一写锁（同库共存域共享同一实例）。"""

        return self._lock

    def close(self) -> None:
        with self._lock:
            if self._owns_connection:
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
        return WorkflowStore.get_workflow(self, workflow_uuid)

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
        workflow_type: Optional[str] = None,
        operation_category_uuid: Optional[str] = None,
        published: Optional[bool] = None,
    ) -> Dict[str, Any]:
        where = "deleted_at IS NULL"
        values: List[Any] = []
        if name:
            where += " AND name LIKE ?"
            values.append(f"%{name}%")
        if workflow_type is not None:
            where += " AND workflow_type = ?"
            values.append(workflow_type)
        if operation_category_uuid is not None:
            where += " AND operation_category_uuid = ?"
            values.append(operation_category_uuid)
        if published is not None:
            # V1 status：当前修订已发布为 published，否则 source
            where += (
                (" AND " if published else " AND NOT ")
                + "EXISTS (SELECT 1 FROM workflow_publication AS publication "
                "WHERE publication.workflow_uuid = workflow.uuid "
                "AND publication.revision = workflow.revision)"
            )
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
            WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
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
        return WorkflowStore.get_workflow(self, workflow_uuid)

    def delete_workflow(self, workflow_uuid: str) -> None:
        now = utc_now()
        with self.transaction() as conn:
            WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
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
        workflow = WorkflowStore.get_workflow(self, workflow_uuid, conn=database)
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

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        """在同一 SQLite 锁视图中冻结应用图和 Authoring 源码事实。"""

        with self._lock:
            graph = WorkflowStore.get_graph(self, workflow_uuid, conn=self._conn)
            row = self._conn.execute(
                "SELECT applied_source FROM workflow_authoring "
                "WHERE workflow_uuid = ?",
                (workflow_uuid,),
            ).fetchone()
            applied_source = (
                _load(row["applied_source"], None) if row is not None else None
            )
            return {**graph, "applied_source": applied_source}

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite],
        edges: List[WorkflowEdgeWrite],
        protect_reserved_metadata: bool = False,
        workflow_io: Optional[Dict[str, Any]] = None,
        resource_scopes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        with self.transaction() as conn:
            if resource_scopes is not None:
                from unilabos.protocol.runtime.resource_intervals import WorkflowResourceScopesRequest
                checked_scopes = WorkflowResourceScopesRequest(revision=revision, scopes=resource_scopes)
                current = WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
                metadata = dict(current["meta_data"])
                metadata["unilab"] = {**metadata.get("unilab", {}), "resource_scopes":
                    [scope.model_dump(exclude_none=True) for scope in checked_scopes.scopes]}
                conn.execute("UPDATE workflow SET meta_data=? WHERE uuid=?", (_json(metadata), workflow_uuid))
            if workflow_io is not None:
                if set(workflow_io) != {"input_contract", "output_contract", "output_bindings"}:
                    raise StoreConflict("工作流 I/O 必须完整声明输入、输出合同与输出绑定")
                current = WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
                metadata = dict(current["meta_data"])
                metadata["unilab"] = {**metadata.get("unilab", {}), **workflow_io}
                conn.execute("UPDATE workflow SET meta_data=? WHERE uuid=?", (_json(metadata), workflow_uuid))
            self._reconcile_graph(
                conn,
                workflow_uuid=workflow_uuid,
                expected_revision=revision,
                nodes=nodes,
                edges=edges,
                advance_revision=True,
                protect_reserved_metadata=protect_reserved_metadata,
            )
            graph = WorkflowStore.get_graph(self, workflow_uuid, conn=conn)
            unilab = (graph["workflow"].get("meta_data") or {}).get("unilab") or {}
            if unilab.get("resource_scopes"):
                from unilabos.server.backend.scheduler.interval_plan import validate_graph_resource_scopes
                try:
                    validate_graph_resource_scopes(graph)
                except (ValueError, TypeError) as exc:
                    raise StoreConflict(str(exc)) from exc
            if "input_contract" in unilab or "output_contract" in unilab:
                from .workflow_io import validate_workflow_graph_io, WorkflowIOValidationError

                try:
                    checked = validate_workflow_graph_io(graph)
                except WorkflowIOValidationError as exc:
                    raise StoreConflict(f"{exc.code}（{exc.path}）") from exc
                metadata = dict(graph["workflow"]["meta_data"])
                metadata["unilab"] = {**unilab, "input_contract": checked.input_contract.to_dict(),
                    "output_contract": checked.output_contract.to_dict(), "output_bindings": checked.output_bindings}
                conn.execute("UPDATE workflow SET meta_data=? WHERE uuid=?", (_json(metadata), workflow_uuid))
        return WorkflowStore.get_graph(self, workflow_uuid)

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
        workflow = WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
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
            _json(node.manual_confirmation),
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO workflow_node(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, workflow_node_template_uuid,
                    parent_uuid, material_uuid, name, status, type, icon, pose,
                    param, footer, action_name, action_type, execution_policy,
                    disabled, minimized, script, manual_confirmation
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
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
                minimized = ?, script = ?, manual_confirmation = ?
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

    # Workflow 发布与组合调用 ---------------------------------------------

    def save_graph_with_reserved_metadata(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        workflow_meta_data: Dict[str, Any],
        nodes: Sequence[Dict[str, Any]],
        edges: Sequence[Dict[str, Any]],
        template_authority_id: str = "",
        node_templates: Sequence[Dict[str, Any]] = (),
        handle_templates: Sequence[Dict[str, Any]] = (),
    ) -> Dict[str, Any]:
        """服务端写入保留的 ``meta_data.unilab``（I/O 声明、组合调用），其余同全图协调。

        与 ``save_graph`` 共用 revision 乐观锁、图校验与 revision 递增；``node_templates``
        是要随图一起落库的已发布工作流模板（按 uuid upsert）。
        """

        node_values = [
            WorkflowNodeWrite.model_validate(
                {field: item[field] for field in WorkflowNodeWrite.model_fields if field in item}
            )
            for item in nodes
        ]
        edge_values = [
            WorkflowEdgeWrite.model_validate(
                {field: item[field] for field in WorkflowEdgeWrite.model_fields if field in item}
            )
            for item in edges
        ]
        now = utc_now()
        with self.transaction() as conn:
            if node_templates:
                self._upsert_published_templates(
                    conn,
                    node_templates,
                    handle_templates,
                    authority_id=template_authority_id,
                    now=now,
                )
            self._reconcile_graph(
                conn,
                workflow_uuid=workflow_uuid,
                expected_revision=revision,
                nodes=node_values,
                edges=edge_values,
                advance_revision=True,
                protect_reserved_metadata=False,
                semantic_workflow_meta_data=workflow_meta_data,
            )
            conn.execute(
                "UPDATE workflow SET meta_data = ?, update_time = ? "
                "WHERE uuid = ? AND deleted_at IS NULL",
                (_json(workflow_meta_data), now, workflow_uuid),
            )
        return WorkflowStore.get_graph(self, workflow_uuid)

    def _upsert_published_templates(
        self,
        conn: sqlite3.Connection,
        node_templates: Sequence[Dict[str, Any]],
        handle_templates: Sequence[Dict[str, Any]],
        *,
        authority_id: str,
        now: str,
    ) -> None:
        """按 uuid 写入已发布工作流模板；同模板下不在新集合里的 Handle 软删除。"""

        if not authority_id:
            raise StoreConflict("published template authority_id must not be empty")
        try:
            for template in node_templates:
                schema = template.get("schema")
                if schema is not None and not isinstance(schema, str):
                    schema = _json(schema)
                values = (
                    template.get("description"),
                    _json(template.get("meta_data") or {}),
                    authority_id,
                    template["resource_template_uuid"],
                    template["name"],
                    template.get("display_name") or "",
                    template.get("class"),
                    _json(template.get("goal") or {}),
                    _json(template.get("goal_default") or {}),
                    _json(template.get("feedback") or {}),
                    _json(template.get("result") or {}),
                    schema,
                    template["type"],
                    template["node_type"],
                )
                existing = conn.execute(
                    "SELECT uuid FROM workflow_node_template WHERE uuid = ?",
                    (template["uuid"],),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO workflow_node_template(
                            uuid, create_time, update_time, deleted_at, description,
                            meta_data, authority_id, resource_template_uuid, name,
                            display_name, class, goal, goal_default, feedback, result,
                            schema, type, node_type
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (template["uuid"], now, now, *values),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE workflow_node_template
                        SET update_time = ?, deleted_at = NULL, description = ?,
                            meta_data = ?, authority_id = ?, resource_template_uuid = ?,
                            name = ?, display_name = ?, class = ?, goal = ?,
                            goal_default = ?, feedback = ?, result = ?, schema = ?,
                            type = ?, node_type = ?
                        WHERE uuid = ?
                        """,
                        (now, *values, template["uuid"]),
                    )
                retained = [
                    str(handle["uuid"])
                    for handle in handle_templates
                    if handle.get("workflow_node_template_uuid") == template["uuid"]
                ]
                marks = ",".join("?" for _ in retained) or "''"
                conn.execute(
                    f"""
                    UPDATE workflow_handle_template
                    SET deleted_at = ?, update_time = ?
                    WHERE workflow_node_template_uuid = ? AND deleted_at IS NULL
                      AND uuid NOT IN ({marks})
                    """,
                    (now, now, template["uuid"], *retained),
                )
            for handle in handle_templates:
                values = (
                    handle.get("description"),
                    _json(handle.get("meta_data") or {}),
                    authority_id,
                    handle["workflow_node_template_uuid"],
                    handle["handle_key"],
                    handle["io_type"],
                    handle.get("display_name") or "",
                    handle["type"],
                    int(bool(handle.get("required"))),
                    handle.get("data_source"),
                    handle.get("data_key"),
                )
                existing = conn.execute(
                    "SELECT uuid FROM workflow_handle_template WHERE uuid = ?",
                    (handle["uuid"],),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO workflow_handle_template(
                            uuid, create_time, update_time, deleted_at, description,
                            meta_data, authority_id, workflow_node_template_uuid,
                            handle_key, io_type, display_name, type, required,
                            data_source, data_key
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (handle["uuid"], now, now, *values),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE workflow_handle_template
                        SET update_time = ?, deleted_at = NULL, description = ?,
                            meta_data = ?, authority_id = ?,
                            workflow_node_template_uuid = ?, handle_key = ?,
                            io_type = ?, display_name = ?, type = ?, required = ?,
                            data_source = ?, data_key = ?
                        WHERE uuid = ?
                        """,
                        (now, *values, handle["uuid"]),
                    )
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"published workflow template conflicts: {exc}") from exc

    def list_catalog_templates(
        self,
        template_uuids: Iterable[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """读取当前目录中的普通（非已发布工作流）节点模板及其 Handle。"""

        identities = sorted(set(template_uuids))
        if not identities:
            return [], []
        marks = ",".join("?" for _ in identities)
        with self._lock:
            template_rows = self._conn.execute(
                f"""
                SELECT * FROM workflow_node_template
                WHERE uuid IN ({marks}) AND deleted_at IS NULL
                  AND node_type <> 'workflow'
                ORDER BY uuid
                """,
                identities,
            ).fetchall()
            found = [row["uuid"] for row in template_rows]
            handle_rows = (
                self._conn.execute(
                    f"""
                    SELECT * FROM workflow_handle_template
                    WHERE workflow_node_template_uuid IN ({",".join("?" for _ in found)})
                      AND deleted_at IS NULL
                    ORDER BY uuid
                    """,
                    found,
                ).fetchall()
                if found
                else []
            )
        return (
            [self._node_template_row(row) for row in template_rows],
            [self._handle_template_row(row) for row in handle_rows],
        )

    def insert_workflow_publication(
        self,
        *,
        workflow_uuid: str,
        revision: int,
        name: str,
        source_hash: str,
        contract_digest: str,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """写入一次发布；同一 (workflow_uuid, revision) 已存在时返回既有行。"""

        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO workflow_publication(
                        uuid, create_time, workflow_uuid, revision, name,
                        source_hash, contract_digest, snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        utc_now(),
                        workflow_uuid,
                        revision,
                        name,
                        source_hash,
                        contract_digest,
                        _json(snapshot),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = WorkflowStore.find_workflow_publication(self, workflow_uuid, revision)
            if existing is None:
                raise StoreConflict(f"workflow publication rejected: {exc}") from exc
            return existing
        found = WorkflowStore.find_workflow_publication(self, workflow_uuid, revision)
        assert found is not None
        return found

    def find_workflow_publication(
        self,
        workflow_uuid: str,
        revision: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """按修订读取发布；``revision`` 为空时取最新一次发布。"""

        with self._lock:
            if revision is None:
                row = self._conn.execute(
                    "SELECT * FROM workflow_publication WHERE workflow_uuid = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (workflow_uuid,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM workflow_publication "
                    "WHERE workflow_uuid = ? AND revision = ?",
                    (workflow_uuid, revision),
                ).fetchone()
        return self._publication_row(row) if row is not None else None

    def list_workflow_publications(self, workflow_uuid: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_publication WHERE workflow_uuid = ? "
                "ORDER BY revision DESC",
                (workflow_uuid,),
            ).fetchall()
        return [self._publication_row(row) for row in rows]

    def list_latest_workflow_publications(self) -> List[Dict[str, Any]]:
        """每个未删除工作流的最新一次发布（调色板数据源）。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT publication.* FROM workflow_publication AS publication
                JOIN workflow ON workflow.uuid = publication.workflow_uuid
                WHERE workflow.deleted_at IS NULL
                  AND publication.revision = (
                      SELECT MAX(latest.revision) FROM workflow_publication AS latest
                      WHERE latest.workflow_uuid = publication.workflow_uuid
                  )
                ORDER BY publication.create_time DESC, publication.uuid
                """
            ).fetchall()
        return [self._publication_row(row) for row in rows]

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
        idempotency_key: Optional[str] = None,
        request_fingerprint: str = "",
        input_builder: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        priority: str = "normal",
        inventory_bindings: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """``input_builder`` 按同一事务内的图求有效任务输入（先于 ``plan_builder`` 调用）。

        ``priority`` / ``inventory_bindings`` 是 V1 创建字段，随任务原样冻结。
        """

        now = utc_now()
        with self.transaction() as conn:
            graph = WorkflowStore.get_graph(self, workflow_uuid, conn=conn)
            if input_builder is not None:
                input_value = input_builder(graph)
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
                    cleanup_status, trace_context, input, output, error_info,
                    idempotency_key, request_fingerprint, priority, inventory_bindings
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'pending', ?, ?, ?, ?, ?,
                          'none', '{}', ?, '{}', '[]', ?, ?, ?, ?)
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
                    idempotency_key,
                    request_fingerprint if idempotency_key is not None else "",
                    priority,
                    _json(list(inventory_bindings or [])),
                ),
            )
            for spec in jobs:
                self._insert_node_run(conn, task_uuid=task_uuid, spec=spec, now=now)
        return self.get_task(task_uuid)

    # 节点运行（run）与 attempt（job）-------------------------------------------

    _RUN_TERMINAL = frozenset({"succeeded", "failed", "skipped", "canceled", "timeout"})
    _RUN_IN_FLIGHT = frozenset(
        {"dispatched", "running", "intervention_required", "cancel_requested", "execution_unknown"}
    )

    def _insert_node_run(
        self,
        conn: sqlite3.Connection,
        *,
        task_uuid: str,
        spec: Dict[str, Any],
        now: str,
    ) -> str:
        """写入一个节点运行及其 attempt 1；整图任务与单点任务共用。

        ``spec["uuid"]`` 是节点运行（DAG 节点）的稳定身份，attempt 另取新 uuid。
        """

        run_uuid = str(spec["uuid"])
        job_uuid = str(uuid4())
        conn.execute(
            """
            INSERT INTO workflow_node_run(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                workflow_task_uuid, workflow_node_uuid, topological_index,
                executor_kind, execution_policy, execution_timeout_seconds, param,
                material_uuid, status, current_job_uuid, attempt_count,
                return_info, error_info, feedback_data, control_data
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 1,
                      '{}', '[]', '{}', '{}')
            """,
            (
                run_uuid,
                now,
                now,
                task_uuid,
                spec["workflow_node_uuid"],
                int(spec.get("topological_index") or 0),
                spec["executor_kind"],
                _json(spec.get("execution_policy") or {}),
                int(spec.get("execution_timeout_seconds") or 0),
                _json(spec.get("param") or {}),
                spec.get("material_uuid"),
                job_uuid,
            ),
        )
        self._insert_attempt(
            conn,
            job_uuid=job_uuid,
            run_uuid=run_uuid,
            task_uuid=task_uuid,
            node_uuid=str(spec["workflow_node_uuid"]),
            attempt_no=1,
            retry_of_job_uuid=None,
            trigger="initial",
            param=spec.get("param") or {},
            now=now,
        )
        return run_uuid

    @staticmethod
    def _insert_attempt(
        conn: sqlite3.Connection,
        *,
        job_uuid: str,
        run_uuid: str,
        task_uuid: str,
        node_uuid: str,
        attempt_no: int,
        retry_of_job_uuid: Optional[str],
        trigger: str,
        param: Dict[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                workflow_node_run_uuid, workflow_task_uuid, workflow_node_uuid,
                attempt_no, retry_of_job_uuid, trigger, feedback_sequence, status, param,
                feedback_data, return_info, error_resolution, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, ?, ?, ?, 0, 'pending', ?,
                      '{}', '{}', '{}', '{}', '[]')
            """,
            (
                job_uuid,
                now,
                now,
                run_uuid,
                task_uuid,
                node_uuid,
                int(attempt_no),
                retry_of_job_uuid,
                trigger,
                _json(param),
            ),
        )

    def _sync_run_projection(
        self, conn: sqlite3.Connection, run_uuid: str, now: str
    ) -> Dict[str, Any]:
        """把当前 attempt 的状态/结果投影到节点运行，并发节点运行变更事件。"""

        run = conn.execute(
            "SELECT * FROM workflow_node_run WHERE uuid=? AND deleted_at IS NULL",
            (run_uuid,),
        ).fetchone()
        if run is None:
            raise StoreNotFound(f"workflow node run {run_uuid} not found")
        job = conn.execute(
            "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
            (run["current_job_uuid"],),
        ).fetchone()
        if job is None:
            raise StoreConflict(f"workflow node run {run_uuid} has no current attempt")
        finished_at = job["finished_at"] if job["status"] in self._RUN_TERMINAL else None
        conn.execute(
            """
            UPDATE workflow_node_run
            SET status=?, return_info=?, error_info=?, feedback_data=?,
                started_at=COALESCE(started_at, ?), finished_at=?, update_time=?
            WHERE uuid=?
            """,
            (
                job["status"],
                job["return_info"],
                job["error_info"],
                job["feedback_data"],
                job["started_at"],
                finished_at,
                now,
                run_uuid,
            ),
        )
        self._append_event(
            conn,
            event="workflow.node_run.changed",
            data={
                "workflow_node_run_uuid": run_uuid,
                "workflow_task_uuid": run["workflow_task_uuid"],
                "workflow_node_uuid": run["workflow_node_uuid"],
                "status": job["status"],
                "current_job_uuid": job["uuid"],
                "attempt_count": int(run["attempt_count"]),
            },
            now=now,
        )
        return self._run_row(
            conn.execute(
                "SELECT * FROM workflow_node_run WHERE uuid=?", (run_uuid,)
            ).fetchone()
        )

    def _emit_job_changed(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        status: str,
        now: str,
        **extra: Any,
    ) -> None:
        self._append_event(
            conn,
            event="workflow.node_job.changed",
            data={
                "workflow_node_job_uuid": job["uuid"],
                "workflow_node_run_uuid": job["workflow_node_run_uuid"],
                "attempt_no": int(job["attempt_no"]),
                "status": status,
                **extra,
            },
            now=now,
        )

    def create_ad_hoc_task_with_job(
        self,
        *,
        task_uuid: str,
        node_uuid: str,
        device_id: str,
        action_name: str,
        action_type: str,
        param: Dict[str, Any],
        execution_policy: Dict[str, Any],
        execution_timeout_seconds: int,
        description: Optional[str],
        meta_data: Dict[str, Any],
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Dict[str, Any]:
        """单点设备动作任务：无 workflow 定义，task + 单个节点运行（attempt 1）一次落库。

        snapshot/plan 按调度器 `_build_dag` 的消费面构造（snapshot.nodes 提供
        action_name/action_type/target_device_id，plan.nodes 提供 param），
        与整图任务共用同一执行/历史/异常链路。幂等键唯一索引兜底并发重复。
        """

        now = utc_now()
        snapshot = {
            "nodes": [
                {
                    "uuid": node_uuid,
                    "action_name": action_name,
                    "action_type": action_type,
                    "meta_data": {"target_device_id": device_id},
                }
            ],
            "edges": [],
        }
        plan = {
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [{"uuid": node_uuid, "param": param}],
            "edges": [],
        }
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO workflow_task(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, workflow_uuid, status, workflow_snapshot,
                        execution_plan, run_mode, target_node_uuid, control_status,
                        cleanup_status, trace_context, input, output, error_info,
                        execution_kind, idempotency_key, request_fingerprint
                    ) VALUES (?, ?, ?, NULL, ?, ?, NULL, 'pending', ?, ?, 'normal',
                              NULL, 'active', 'none', '{}', '{}', '{}', '[]',
                              'ad_hoc_device_action', ?, ?)
                    """,
                    (
                        task_uuid,
                        now,
                        now,
                        description,
                        _json(meta_data),
                        _json(snapshot),
                        _json(plan),
                        idempotency_key,
                        request_fingerprint,
                    ),
                )
                self._insert_node_run(
                    conn,
                    task_uuid=task_uuid,
                    spec={
                        "uuid": str(uuid4()),
                        "workflow_node_uuid": node_uuid,
                        "topological_index": 0,
                        "executor_kind": "device_action",
                        "execution_policy": execution_policy,
                        "execution_timeout_seconds": execution_timeout_seconds,
                        "param": param,
                    },
                    now=now,
                )
        except sqlite3.IntegrityError as exc:
            # 幂等键唯一索引命中：并发同键提交由 service 读回先到者
            raise StoreConflict(str(exc)) from exc
        return self.get_task(task_uuid)

    def find_task_by_idempotency_key(
        self, execution_kind: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        """按 (execution_kind, idempotency_key) 命中既有任务（幂等复用）。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_task
                WHERE execution_kind = ? AND idempotency_key = ?
                  AND deleted_at IS NULL
                """,
                (execution_kind, idempotency_key),
            ).fetchone()
        return None if row is None else self._task_row(row)

    def get_task(self, task_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            return {
                **self._task_row(row),
                "control_revision": self._task_control_revision(self._conn, task_uuid),
            }

    @staticmethod
    def _task_control_revision(conn: sqlite3.Connection, task_uuid: str) -> int:
        # 复用已有命令日志，不增加第二套控制状态表。每条已受理命令推进一次版本。
        return int(conn.execute(
            "SELECT COUNT(*) FROM workflow_task_command "
            "WHERE workflow_task_uuid=? AND deleted_at IS NULL", (task_uuid,)
        ).fetchone()[0])

    def _apply_pause(
        self, conn: sqlite3.Connection, task: Dict[str, Any], *,
        idempotency_key: str, expected_revision: int, now: str,
    ) -> None:
        """暂停 = 转单步并关上派发闸门。

        已下发的动作照常跑完、继续持有资源；尚未下发的节点（含排队等锁的）不再派发，
        之后由 step 逐个放行或 resume 转回自动。等待裁决中的任务记下恢复目标为 paused。
        """

        task_uuid = task["uuid"]
        control = task["control_status"]
        if task["run_mode"] == "step" and control == "paused":
            raise StoreConflict("task is already paused")
        if control == "waiting_reconciliation":
            conn.execute(
                "UPDATE workflow_task SET run_mode='step', "
                "reconciliation_resume_control_status='paused', update_time=? WHERE uuid=?",
                (now, task_uuid),
            )
        elif control == "active":
            # 收回尚未用掉的步进许可；已认领且已下发的那一步照常跑完，完成时转 paused。
            executing = False
            for permit in conn.execute(
                "SELECT uuid, result FROM workflow_task_command WHERE workflow_task_uuid=? "
                "AND type='step' AND status='pending' AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchall():
                job_uuid = _load(permit["result"], {}).get("job_uuid")
                if job_uuid:
                    job = conn.execute(
                        "SELECT status FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                        (job_uuid,),
                    ).fetchone()
                    if job is not None and job["status"] != "pending":
                        executing = True
                        continue
                conn.execute(
                    "UPDATE workflow_task_command SET status='rejected', consumed_at=?, "
                    "update_time=?, result=json_set(result, '$.revoked_by', 'pause') WHERE uuid=?",
                    (now, now, permit["uuid"]),
                )
            conn.execute(
                "UPDATE workflow_task SET run_mode='step', control_status=?, update_time=? WHERE uuid=?",
                ("active" if executing else "paused", now, task_uuid),
            )
        else:
            raise StoreConflict("task requires intervention before it can be paused")
        conn.execute(
            "INSERT INTO workflow_task_command(uuid, create_time, update_time, "
            "workflow_task_uuid, type, idempotency_key, status, meta_data, consumed_at) "
            "VALUES (?, ?, ?, ?, 'pause', ?, 'succeeded', ?, ?)",
            (str(uuid4()), now, now, task_uuid, idempotency_key,
             _json({"expected_revision": expected_revision}), now),
        )
        self._append_event(conn, event="workflow.task.changed",
                           data={"workflow_task_uuid": task_uuid}, now=now)

    def _apply_cancel(
        self, conn: sqlite3.Connection, task: Dict[str, Any], *,
        idempotency_key: str, expected_revision: int, now: str,
    ) -> None:
        """取消 = 任务进入 canceling：不再派发；已下发的动作等真实返回后任务落 canceled。"""

        if task["control_revision"] != expected_revision:
            raise StoreRevisionConflict("task control revision changed")
        task_uuid = task["uuid"]
        conn.execute(
            "UPDATE workflow_task_command SET status='rejected', consumed_at=?, update_time=?, "
            "result=json_set(result, '$.revoked_by', 'cancel') WHERE workflow_task_uuid=? "
            "AND type='step' AND status='pending' AND deleted_at IS NULL",
            (now, now, task_uuid),
        )
        conn.execute(
            "UPDATE workflow_task SET status='canceling', update_time=? WHERE uuid=?",
            (now, task_uuid),
        )
        conn.execute(
            "INSERT INTO workflow_task_command(uuid, create_time, update_time, "
            "workflow_task_uuid, type, idempotency_key, status, meta_data, consumed_at) "
            "VALUES (?, ?, ?, ?, 'cancel', ?, 'succeeded', ?, ?)",
            (str(uuid4()), now, now, task_uuid, idempotency_key,
             _json({"expected_revision": expected_revision}), now),
        )
        self._append_event(conn, event="workflow.task.changed",
                           data={"workflow_task_uuid": task_uuid, "status": "canceling"}, now=now)

    def edit_task_input(
        self,
        task_uuid: str,
        *,
        expected_revision: int,
        values: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """暂停窗口内修改入口参数（ADR 0002），记为一条 ``edit_input`` 任务命令。

        只改值，不改计划形状：引用该参数的未下发节点运行、其 pending attempt 与计划节点的
        param 同步更新。参数已被任何已下发（非 pending）的 attempt 用过即冻结，整次修改拒绝。
        """

        now = utc_now()
        with self.transaction() as conn:
            task = self.get_task(task_uuid)
            previous = conn.execute(
                "SELECT * FROM workflow_task_command WHERE workflow_task_uuid=? "
                "AND idempotency_key=? AND deleted_at IS NULL",
                (task_uuid, idempotency_key),
            ).fetchone()
            if previous is not None:
                meta = _load(previous["meta_data"], {})
                if (
                    previous["type"] != "edit_input"
                    or meta.get("expected_revision") != expected_revision
                    or not strict_json_equal(meta.get("input"), values)
                ):
                    raise StoreConflict("command idempotency key already used")
                return task
            if task["execution_kind"] != "workflow":
                raise StoreConflict("only workflow tasks accept input edits")
            if task["status"] not in {"pending", "running"}:
                raise StoreConflict("task is no longer executable")
            if task["control_revision"] != expected_revision:
                raise StoreRevisionConflict("task control revision changed")
            if task["control_status"] != "paused":
                raise StoreConflict("入口参数只能在任务暂停时修改")
            current = dict(task["input"])
            changed = {
                name: value
                for name, value in values.items()
                if name not in current or not strict_json_equal(current[name], value)
            }
            plan = task["execution_plan"]
            affected = []
            for planned in plan.get("nodes") or []:
                names = set(planned.get("entry_parameter_names") or []) | set((planned.get("entry_bindings") or {}).values())
                used = names & changed.keys()
                if used:
                    affected.append((planned, used))
            runs = {
                row["workflow_node_uuid"]: row
                for row in conn.execute(
                    "SELECT * FROM workflow_node_run WHERE workflow_task_uuid=? AND deleted_at IS NULL",
                    (task_uuid,),
                ).fetchall()
            }
            for planned, names in affected:
                run = runs.get(planned["uuid"])
                if run is None:
                    continue
                name = sorted(names)[0]
                if run["executor_kind"] == "loop":
                    raise StoreConflict(f"入口参数 {name!r} 被循环容器使用，运行中不可修改")
                dispatched = conn.execute(
                    "SELECT 1 FROM workflow_node_job WHERE workflow_node_run_uuid=? "
                    "AND deleted_at IS NULL AND (status <> 'pending' OR started_at IS NOT NULL "
                    "OR json_type(control_data,'$.inventory_request') IS NOT NULL) LIMIT 1",
                    (run["uuid"],),
                ).fetchone()
                if dispatched is not None:
                    raise StoreConflict(f"入口参数 {name!r} 已被已下发的节点使用，不能修改")
            for planned, _names in affected:
                if "entry_param_template" in planned:
                    from .entry_parameters import substitute_entry_references

                    if planned.get("kind") != "condition":
                        planned["param"], _bindings = substitute_entry_references(
                            planned["entry_param_template"], {**current, **values})
                else:
                    # 兼容旧任务快照：旧计划只有顶层参数的绑定表。
                    planned["param"] = {**(planned.get("param") or {}), **{
                        key: changed[name] for key, name in (planned.get("entry_bindings") or {}).items() if name in changed}}
                run = runs.get(planned["uuid"])
                if run is None:
                    continue
                conn.execute(
                    "UPDATE workflow_node_run SET param=?, update_time=? WHERE uuid=?",
                    (_json(planned["param"]), now, run["uuid"]),
                )
                conn.execute(
                    "UPDATE workflow_node_job SET param=?, update_time=? WHERE workflow_node_run_uuid=? "
                    "AND deleted_at IS NULL AND status='pending'",
                    (_json(planned["param"]), now, run["uuid"]),
                )
            conn.execute(
                "UPDATE workflow_task SET input=?, execution_plan=?, update_time=? WHERE uuid=?",
                (_json({**current, **values}), _json(plan), now, task_uuid),
            )
            conn.execute(
                "INSERT INTO workflow_task_command(uuid, create_time, update_time, "
                "workflow_task_uuid, type, idempotency_key, status, meta_data, result, consumed_at) "
                "VALUES (?, ?, ?, ?, 'edit_input', ?, 'succeeded', ?, ?, ?)",
                (
                    str(uuid4()), now, now, task_uuid, idempotency_key,
                    _json({"expected_revision": expected_revision, "input": values}),
                    _json({
                        "changed": sorted(changed),
                        "previous": {name: current[name] for name in sorted(changed) if name in current},
                        "nodes": sorted({planned["uuid"] for planned, _names in affected}),
                    }),
                    now,
                ),
            )
            self._append_event(conn, event="workflow.task.changed",
                               data={"workflow_task_uuid": task_uuid}, now=now)
        return self.get_task(task_uuid)

    def mark_job_cancel_requested(self, job_uuid: str) -> bool:
        """已下发的 attempt 收到取消请求：展示为 cancel_requested，直到真实终态回报。"""

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None or row["status"] not in {"dispatched", "running"}:
                return False
            conn.execute(
                "UPDATE workflow_node_job SET status='cancel_requested', update_time=? WHERE uuid=?",
                (now, job_uuid),
            )
            self._emit_job_changed(conn, row, "cancel_requested", now)
            self._sync_run_projection(conn, row["workflow_node_run_uuid"], now)
        return True

    def claim_task_step(self, task_uuid: str, job_uuid: str) -> bool:
        """在申请动作锁前消费一次许可；一个许可只能绑定一个 attempt。"""

        with self.transaction() as conn:
            task = self.get_task(task_uuid)
            if task["status"] not in {"pending", "running"}:
                return False  # canceling / 终态：不再派发
            # V1 工站手动处理期间整站暂停普通派发，只放行本工站会话的手动动作
            if (task["meta_data"].get("execution_source") != "error_manual"
                    and conn.execute("SELECT 1 FROM workflow_station_control WHERE mode='PAUSED' LIMIT 1").fetchone()):
                return False
            if task["run_mode"] != "step":
                # V1 pause：普通模式关派发闸门，执行模式不变
                return task.get("dispatch_gate", "active") != "paused"
            if task["control_status"] != "active":
                return False
            command = conn.execute(
                "SELECT * FROM workflow_task_command WHERE workflow_task_uuid=? "
                "AND type='step' AND status='pending' AND deleted_at IS NULL ORDER BY rowid LIMIT 1",
                (task_uuid,),
            ).fetchone()
            if command is None:
                return False
            result = _load(command["result"], {})
            if result.get("job_uuid"):
                return result["job_uuid"] == job_uuid
            job = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND workflow_task_uuid=? "
                "AND status='pending' AND deleted_at IS NULL", (job_uuid, task_uuid),
            ).fetchone()
            if job is None:
                return False
            # V1 step 指定了目标节点时，许可只给该节点
            if command["target_node_uuid"] and command["target_node_uuid"] != job["workflow_node_uuid"]:
                return False
            conn.execute(
                "UPDATE workflow_task_command SET target_node_uuid=?, result=?, update_time=? WHERE uuid=?",
                (job["workflow_node_uuid"], _json({"job_uuid": job_uuid,
                 "node_run_uuid": job["workflow_node_run_uuid"]}), utc_now(), command["uuid"]),
            )
            return True

    def _finish_task_step(self, conn: sqlite3.Connection, job: sqlite3.Row, now: str) -> None:
        """与 attempt 终态同事务收回许可；重试的新 attempt 必须再次获准。"""

        changed = conn.execute(
            "UPDATE workflow_task_command SET status='succeeded', consumed_at=?, update_time=? "
            "WHERE workflow_task_uuid=? AND type='step' AND status='pending' AND deleted_at IS NULL "
            "AND json_extract(result, '$.job_uuid')=?",
            (now, now, job["workflow_task_uuid"], job["uuid"]),
        ).rowcount
        if not changed:
            return
        conn.execute(
            "UPDATE workflow_task SET control_status=CASE WHEN control_status='waiting_reconciliation' "
            "THEN control_status ELSE 'paused' END, reconciliation_resume_control_status="
            "CASE WHEN control_status='waiting_reconciliation' THEN 'paused' "
            "ELSE reconciliation_resume_control_status END, update_time=? "
            "WHERE uuid=? AND run_mode='step'",
            (now, job["workflow_task_uuid"]),
        )
        self._append_event(conn, event="workflow.task.changed",
                           data={"workflow_task_uuid": job["workflow_task_uuid"]}, now=now)

    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        workflow_uuid: Optional[str] = None,
        status: str = "",
        cleanup_status: str = "",
        execution_kind: str = "",
    ) -> Dict[str, Any]:
        clauses = ["deleted_at IS NULL"]
        values: List[Any] = []
        for field, value in (
            ("workflow_uuid", workflow_uuid),
            ("status", status),
            ("cleanup_status", cleanup_status),
            ("execution_kind", execution_kind),
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
            "has_more": offset + len(rows) < total,
        }

    def list_node_runs(
        self, task_uuid: str, *, include_attempts: bool = True
    ) -> List[Dict[str, Any]]:
        """任务的节点运行，每个工作流节点一条，按拓扑序；可内嵌 attempt 历史。"""

        self.get_task(task_uuid)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM workflow_node_run
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                ORDER BY topological_index, create_time, uuid
                """,
                (task_uuid,),
            ).fetchall()
            runs = [self._run_row(row) for row in rows]
            if include_attempts:
                attempts = self._attempts_by_run(
                    self._conn,
                    "job.workflow_task_uuid = ?",
                    (task_uuid,),
                )
                for run in runs:
                    run["attempts"] = attempts.get(run["uuid"], [])
        return runs

    def get_node_run(
        self, run_uuid: str, *, include_attempts: bool = True
    ) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_node_run WHERE uuid = ? AND deleted_at IS NULL",
                (run_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node run {run_uuid} not found")
            run = self._run_row(row)
            if include_attempts:
                run["attempts"] = self._attempts_by_run(
                    self._conn,
                    "job.workflow_node_run_uuid = ?",
                    (run_uuid,),
                ).get(run_uuid, [])
        return run

    def _attempts_by_run(
        self,
        conn: sqlite3.Connection,
        where: str,
        params: tuple,
    ) -> Dict[str, List[Dict[str, Any]]]:
        rows = conn.execute(
            f"""
            SELECT job.* FROM workflow_node_job AS job
            WHERE {where} AND job.deleted_at IS NULL
            ORDER BY job.workflow_node_run_uuid, job.attempt_no
            """,
            params,
        ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["workflow_node_run_uuid"], []).append(self._job_row(row))
        return grouped

    def list_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        """任务的全部 attempt（物理执行）平铺：按节点拓扑序、attempt 序号。"""

        self.get_task(task_uuid)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT job.* FROM workflow_node_job AS job
                JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid
                WHERE job.workflow_task_uuid = ? AND job.deleted_at IS NULL
                ORDER BY run.topological_index, run.create_time, run.uuid, job.attempt_no
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

    def get_manual_confirmation(self, confirmation_uuid: str) -> Dict[str, Any]:
        """读取一条人工确认事实；不把数据库行直接暴露给调用方。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (confirmation_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(
                f"workflow manual confirmation {confirmation_uuid} not found"
            )
        return self._manual_confirmation_row(row)

    def get_manual_confirmation_for_job(
        self, job_uuid: str
    ) -> Optional[Dict[str, Any]]:
        """按 attempt 查确认单；一个 job 最多一张单。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE workflow_node_job_uuid = ? AND deleted_at IS NULL
                ORDER BY create_time, uuid
                LIMIT 1
                """,
                (job_uuid,),
            ).fetchone()
        return None if row is None else self._manual_confirmation_row(row)

    @staticmethod
    def _normalize_manual_assignees(value: Any) -> List[str]:
        """校验并规范人工确认指派列表；显式 ``[]`` 是合法且有意义的值。"""

        if not isinstance(value, list):
            raise StoreConflict("assignee_user_ids must be an explicit list")
        result: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise StoreConflict("assignee_user_ids must contain strings")
            normalized = item.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _manual_decision_status(action: str) -> tuple[str, str]:
        """返回持久化状态与审计用的规范动作名。"""

        normalized = str(action or "").strip().lower()
        aliases = {
            "approve": ("approved", "approve"),
            "approved": ("approved", "approve"),
            "confirm": ("approved", "approve"),
            "confirmed": ("approved", "approve"),
            "skip": ("approved", "skip"),
            "skipped": ("approved", "skip"),
            "reject": ("rejected", "reject"),
            "rejected": ("rejected", "reject"),
            "deny": ("rejected", "reject"),
            "cancel": ("canceled", "cancel"),
            "canceled": ("canceled", "cancel"),
            "timeout": ("timed_out", "timeout"),
            "timed_out": ("timed_out", "timeout"),
            "expire": ("timed_out", "timeout"),
            "expired": ("timed_out", "timeout"),
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise StoreConflict(f"unsupported manual confirmation action: {action!r}") from exc

    def open_manual_confirmation(
        self,
        job_uuid: str,
        *,
        param: Dict[str, Any],
        description: Optional[str] = None,
        meta_data: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[int] = 3600,
        confirmation_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """为一个 pending attempt 建立 durable 人工确认闸门。

        这里刻意要求 ``param`` 中存在 ``assignee_user_ids``：缺 key 代表编辑器
        尚未完成配置，显式 ``[]`` 才代表“不指派，任何人可确认”。数据库列本身
        始终是 JSON array，完整原始动作参数则保存在 ``param``。
        """

        if not isinstance(param, dict):
            raise StoreConflict("manual confirmation param must be an object")
        if "assignee_user_ids" not in param:
            raise StoreConflict(
                "manual confirmation requires explicit assignee_user_ids (use [] for unrestricted)"
            )
        assignees = self._normalize_manual_assignees(param["assignee_user_ids"])
        normalized_param = dict(param)
        normalized_param["assignee_user_ids"] = assignees
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
                raise StoreConflict("timeout_seconds must be an integer")
            if timeout_seconds <= 0:
                raise StoreConflict("timeout_seconds must be positive")
        if meta_data is not None and not isinstance(meta_data, dict):
            raise StoreConflict("manual confirmation meta_data must be an object")
        normalized_description = (
            str(description).strip() if description is not None else None
        ) or None
        requested_uuid = str(confirmation_uuid or uuid4()).strip()
        if not requested_uuid:
            raise StoreConflict("manual confirmation uuid must not be blank")

        with self.transaction() as conn:
            job = conn.execute(
                """
                SELECT * FROM workflow_node_job
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (job_uuid,),
            ).fetchone()
            if job is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            task = conn.execute(
                """
                SELECT * FROM workflow_task
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (job["workflow_task_uuid"],),
            ).fetchone()
            if task is None:
                raise StoreNotFound(
                    f"workflow task {job['workflow_task_uuid']} not found"
                )

            existing = conn.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE workflow_node_job_uuid = ? AND deleted_at IS NULL
                ORDER BY create_time, uuid
                LIMIT 1
                """,
                (job_uuid,),
            ).fetchone()
            if existing is not None:
                if existing["workflow_task_uuid"] != job["workflow_task_uuid"]:
                    raise StoreConflict("manual confirmation task/job mismatch")
                return self._manual_confirmation_row(existing)

            if task["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                raise StoreConflict("cannot open confirmation for a terminal task")
            if job["status"] in self._RUN_TERMINAL:
                raise StoreConflict("cannot open confirmation for a terminal job")

            now = utc_now()
            deadline = None
            if timeout_seconds is not None:
                deadline = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=timeout_seconds)
                ).isoformat().replace("+00:00", "Z")
            marker = {
                "uuid": requested_uuid,
                "status": "pending",
                "assignee_user_ids": assignees,
                "opened_at": now,
                "deadline_at": deadline,
            }
            conn.execute(
                """
                INSERT INTO workflow_manual_confirmation(
                    uuid, create_time, update_time, deleted_at, description, meta_data,
                    workflow_task_uuid, workflow_node_job_uuid, status,
                    assignee_user_ids, param, opened_at, deadline_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    requested_uuid,
                    now,
                    now,
                    normalized_description,
                    _json(dict(meta_data or {})),
                    job["workflow_task_uuid"],
                    job_uuid,
                    _json(assignees),
                    _json(normalized_param),
                    now,
                    deadline,
                ),
            )
            control_data = _load(job["control_data"], {})
            if not isinstance(control_data, dict):
                control_data = {}
            control_data["manual_confirmation"] = marker
            conn.execute(
                """
                UPDATE workflow_node_job
                SET control_data = ?, update_time = ?
                WHERE uuid = ?
                """,
                (_json(control_data), now, job_uuid),
            )
            self._emit_job_changed(
                conn,
                job,
                job["status"],
                now,
                manual_confirmation_uuid=requested_uuid,
                manual_confirmation_status="pending",
            )
            run = conn.execute(
                "SELECT * FROM workflow_node_run WHERE uuid = ? AND deleted_at IS NULL",
                (job["workflow_node_run_uuid"],),
            ).fetchone()
            if run is not None:
                run_control = _load(run["control_data"], {})
                if not isinstance(run_control, dict):
                    run_control = {}
                run_control["manual_confirmation"] = marker
                conn.execute(
                    "UPDATE workflow_node_run SET control_data = ?, update_time = ? WHERE uuid = ?",
                    (_json(run_control), now, run["uuid"]),
                )
                self._sync_run_projection(conn, run["uuid"], now)
            self._append_event(
                conn,
                event="workflow.manual_confirmation.changed",
                data={
                    "uuid": requested_uuid,
                    "task_uuid": job["workflow_task_uuid"],
                    "workflow_task_uuid": job["workflow_task_uuid"],
                    "workflow_node_job_uuid": job_uuid,
                    "status": "pending",
                    "assignee_user_ids": assignees,
                },
                now=now,
            )
            row = conn.execute(
                "SELECT * FROM workflow_manual_confirmation WHERE uuid = ?",
                (requested_uuid,),
            ).fetchone()
            assert row is not None
            return self._manual_confirmation_row(row)

    def _transition_manual_confirmation_locked(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        status: str,
        action: str,
        confirmed_by: Optional[str],
        comment: Optional[str],
        decision_idempotency_key: str,
        now: str,
        expired_before_decision: bool = False,
    ) -> Dict[str, Any]:
        metadata = _load(row["meta_data"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["decision_action"] = action
        if expired_before_decision:
            metadata["expired_before_decision"] = True
        conn.execute(
            """
            UPDATE workflow_manual_confirmation
            SET status = ?, confirmed_by = ?, comment = ?,
                decision_idempotency_key = ?, decided_at = ?,
                meta_data = ?, update_time = ?
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (
                status,
                confirmed_by,
                comment,
                decision_idempotency_key,
                now,
                _json(metadata),
                now,
                row["uuid"],
            ),
        )
        job = conn.execute(
            "SELECT * FROM workflow_node_job WHERE uuid = ? AND deleted_at IS NULL",
            (row["workflow_node_job_uuid"],),
        ).fetchone()
        if job is not None:
            control_data = _load(job["control_data"], {})
            if not isinstance(control_data, dict):
                control_data = {}
            marker = control_data.get("manual_confirmation")
            if not isinstance(marker, dict):
                marker = {"uuid": row["uuid"]}
            marker.update(
                {
                    "status": status,
                    "decision_action": action,
                    "confirmed_by": confirmed_by,
                    "comment": comment,
                    "decision_idempotency_key": decision_idempotency_key,
                    "decided_at": now,
                }
            )
            control_data["manual_confirmation"] = marker
            conn.execute(
                "UPDATE workflow_node_job SET control_data = ?, update_time = ? WHERE uuid = ?",
                (_json(control_data), now, job["uuid"]),
            )
            self._emit_job_changed(
                conn,
                job,
                job["status"],
                now,
                manual_confirmation_uuid=row["uuid"],
                manual_confirmation_status=status,
                decision_action=action,
            )
            run = conn.execute(
                "SELECT * FROM workflow_node_run WHERE uuid = ? AND deleted_at IS NULL",
                (job["workflow_node_run_uuid"],),
            ).fetchone()
            if run is not None:
                run_control = _load(run["control_data"], {})
                if not isinstance(run_control, dict):
                    run_control = {}
                run_marker = run_control.get("manual_confirmation")
                if not isinstance(run_marker, dict):
                    run_marker = {"uuid": row["uuid"]}
                run_marker.update(marker)
                run_control["manual_confirmation"] = run_marker
                conn.execute(
                    "UPDATE workflow_node_run SET control_data = ?, update_time = ? WHERE uuid = ?",
                    (_json(run_control), now, run["uuid"]),
                )
                self._sync_run_projection(conn, run["uuid"], now)
        self._append_event(
            conn,
            event="workflow.manual_confirmation.changed",
            data={
                "uuid": row["uuid"],
                "task_uuid": row["workflow_task_uuid"],
                "workflow_task_uuid": row["workflow_task_uuid"],
                "workflow_node_job_uuid": row["workflow_node_job_uuid"],
                "status": status,
                "decision_action": action,
                "confirmed_by": confirmed_by,
            },
            now=now,
        )
        updated = conn.execute(
            "SELECT * FROM workflow_manual_confirmation WHERE uuid = ?",
            (row["uuid"],),
        ).fetchone()
        assert updated is not None
        return self._manual_confirmation_row(updated)

    def decide_manual_confirmation(
        self,
        confirmation_uuid: str,
        *,
        action: str,
        confirmed_by: Optional[str] = None,
        comment: Optional[str] = None,
        decision_idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """原子地记录人工确认决策；调度器随后消费该事实完成 job。"""

        status, normalized_action = self._manual_decision_status(action)
        actor = str(confirmed_by or "").strip() or None
        normalized_comment = (
            str(comment).strip() if comment is not None else None
        ) or None
        supplied_key = (
            str(decision_idempotency_key).strip()
            if decision_idempotency_key is not None
            else None
        )
        if supplied_key == "":
            raise StoreConflict("decision_idempotency_key must not be blank")

        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (confirmation_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(
                    f"workflow manual confirmation {confirmation_uuid} not found"
                )
            stored_key = row["decision_idempotency_key"]
            if row["status"] != "pending":
                if supplied_key is not None and stored_key not in {None, supplied_key}:
                    raise StoreConflict("manual confirmation already has another decision")
                return self._manual_confirmation_row(row)

            assignees = self._normalize_manual_assignees(
                _load(row["assignee_user_ids"], [])
            )
            # 取消/超时由调度器或运维触发，不受人工指派人限制；批准/拒绝/跳过
            # 在非空列表时必须由名单中的用户作出。
            if status not in {"canceled", "timed_out"}:
                effective_actor = actor or ("operator" if not assignees else "")
                if assignees and effective_actor not in assignees:
                    raise StoreConflict("confirmed_by is not an assignee")
                actor = effective_actor or None
            elif actor is None:
                actor = "scheduler"

            now_dt = datetime.now(timezone.utc)
            deadline = _parse_utc(row["deadline_at"])
            expired = deadline is not None and deadline <= now_dt
            if expired and status not in {"canceled", "timed_out"}:
                status = "timed_out"
                normalized_action = "timeout"

            key = supplied_key or str(uuid4())
            collision = conn.execute(
                """
                SELECT uuid FROM workflow_manual_confirmation
                WHERE decision_idempotency_key = ? AND uuid <> ?
                """,
                (key, confirmation_uuid),
            ).fetchone()
            if collision is not None:
                raise StoreConflict("decision_idempotency_key already used")
            return self._transition_manual_confirmation_locked(
                conn,
                row,
                status=status,
                action=normalized_action,
                confirmed_by=actor,
                comment=normalized_comment,
                decision_idempotency_key=key,
                now=utc_now(),
                expired_before_decision=expired,
            )

    def expire_due_manual_confirmations(
        self, *, now: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """把到期 pending 单原子收敛为 timed_out，供 API/调度器 watchdog 调用。"""

        effective_now = now or utc_now()
        effective_dt = _parse_utc(effective_now)
        if effective_dt is None:
            raise StoreConflict("invalid expiration timestamp")
        expired_rows: List[Dict[str, Any]] = []
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE status = 'pending' AND deleted_at IS NULL
                  AND deadline_at IS NOT NULL
                ORDER BY deadline_at, uuid
                """
            ).fetchall()
            for row in rows:
                deadline = _parse_utc(row["deadline_at"])
                if deadline is None or deadline > effective_dt:
                    continue
                expired_rows.append(
                    self._transition_manual_confirmation_locked(
                        conn,
                        row,
                        status="timed_out",
                        action="timeout",
                        confirmed_by="scheduler",
                        comment=None,
                        decision_idempotency_key=str(uuid4()),
                        now=effective_now,
                        expired_before_decision=True,
                    )
                )
        return expired_rows

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

    def list_template_action_references(self) -> List[Dict[str, Any]]:
        """活跃 workflow 节点对模板 action 的引用明细行。

        Registry Authority 用这些引用检测模板变更冲突，并定位受影响的画布
        节点。被引用的 action 删除或定义变化时，对应模板版本进入待确认状态。
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.resource_template_uuid AS template_uuid,
                       t.name AS action_name,
                       n.uuid AS node_uuid,
                       n.name AS node_name,
                       w.uuid AS workflow_uuid,
                       w.name AS workflow_name
                FROM workflow_node n
                JOIN workflow_node_template t
                  ON t.uuid = n.workflow_node_template_uuid
                JOIN workflow w
                  ON w.uuid = n.workflow_uuid
                WHERE n.deleted_at IS NULL
                  AND t.deleted_at IS NULL
                  AND w.deleted_at IS NULL
                ORDER BY w.name, w.uuid, n.name, n.uuid
                """
            ).fetchall()
        return [
            {
                "template_uuid": str(row["template_uuid"]),
                "action": str(row["action_name"]),
                "node_uuid": str(row["node_uuid"]),
                "node_name": str(row["node_name"]),
                "workflow_uuid": str(row["workflow_uuid"]),
                "workflow_name": str(row["workflow_name"]),
            }
            for row in rows
        ]

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

    def mark_job_execution_unknown(self, job_uuid: str, reason: str) -> bool:
        """运行中丢失执行端的 attempt 转 execution_unknown，任务进入 ``waiting_reconciliation``。

        与 :meth:`prepare_task_execution` 对在飞 attempt 的处理一致；返回是否发生了转换
        （attempt 已不在 dispatched / running / cancel_requested 时不改动）。
        """

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT job.workflow_node_run_uuid AS run_uuid, run.workflow_task_uuid AS task_uuid
                FROM workflow_node_job AS job
                JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid
                WHERE job.uuid = ? AND job.deleted_at IS NULL
                """,
                (job_uuid,),
            ).fetchone()
            if row is None:
                return False
            updated = conn.execute(
                """
                UPDATE workflow_node_job
                SET status = 'execution_unknown', uncertainty_reason = ?, update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                  AND status IN ('dispatched', 'running', 'cancel_requested')
                """,
                (reason, now, job_uuid),
            ).rowcount
            if not updated:
                return False
            self._sync_run_projection(conn, row["run_uuid"], now)
            # 托管转待核实、执行声明转不确定，与 execution_unknown 同事务可见。
            from .resource_intervals import ResourceIntervals

            ResourceIntervals.settle_jobs(conn, [job_uuid])
            conn.execute(
                """
                UPDATE workflow_task
                SET reconciliation_resume_control_status = COALESCE(
                        reconciliation_resume_control_status, control_status
                    ),
                    control_status = 'waiting_reconciliation',
                    attention_reason = ?, update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (reason, now, row["task_uuid"]),
            )
            task = conn.execute("SELECT status FROM workflow_task WHERE uuid = ?", (row["task_uuid"],)).fetchone()
            self._append_event(
                conn,
                event="workflow.task.changed",
                data={
                    "workflow_task_uuid": row["task_uuid"],
                    "status": task["status"] if task is not None else "running",
                    "control_status": "waiting_reconciliation",
                },
                now=now,
            )
        return True

    def prepare_task_execution(self, task_uuid: str) -> Dict[str, Any]:
        """认领/恢复任务，并以节点运行终态作为持久 completed cursor。

        判定单元是节点运行（当前 attempt 的投影），历史 failed attempt 不参与。
        已成功节点不会重跑。崩溃时处于 dispatched/running 的 attempt 无法证明设备
        是否已经产生副作用，attempt 与节点运行同时转 execution_unknown，禁止盲目重放；
        任务控制态转 ``waiting_reconciliation`` 并返回同名 state——它表示任务**可以**
        被调度器接管，但这些 attempt 要由人裁决（重试/跳过/替换成功/标记失败），
        调度器据此向用户开决策而不是重新下发。全部裁决完成后由
        :meth:`settle_task_reconciliation` 恢复原控制态。
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
            run_rows = conn.execute(
                "SELECT * FROM workflow_node_run WHERE workflow_task_uuid = ? "
                "AND deleted_at IS NULL ORDER BY topological_index, create_time, uuid",
                (task_uuid,),
            ).fetchall()
            runs = [self._run_row(row) for row in run_rows]
            if task["status"] in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                return {"state": "terminal", "task": task, "runs": runs}
            statuses = {run["status"] for run in runs}
            # 最后一个 attempt 已落终态、runner 尚未收尾时也可能重启。步进暂停不能
            # 阻止任务终态恢复，否则全节点成功却永远停在 paused，且再无动作可放行。
            terminal_ready = not runs or statuses <= {"succeeded", "skipped"} or bool(
                statuses & {"failed", "timeout", "canceled"}
            )
            # 取消中的任务即使处于暂停也要接管：调度器据此把未起跑的节点收为 canceled
            if (task["control_status"] not in {"active", "waiting_reconciliation"}
                    and not terminal_ready and task["status"] != "canceling"):
                return {"state": task["control_status"], "task": task, "runs": runs}

            # 循环容器没有设备副作用：在飞的循环 attempt 直接退回 pending，由调度器按
            # control_data.loop.iteration 续跑，不需要人裁决（循环体里的设备节点仍要）。
            for run in runs:
                if run["status"] in self._RUN_IN_FLIGHT and run["executor_kind"] in {"loop", "condition"}:
                    conn.execute(
                        """
                        UPDATE workflow_node_job
                        SET status = 'pending', update_time = ?
                        WHERE uuid = ? AND deleted_at IS NULL
                          AND status IN ('dispatched', 'running', 'cancel_requested')
                        """,
                        (now, run["current_job_uuid"]),
                    )
                    run["status"] = self._sync_run_projection(conn, run["uuid"], now)["status"]
            statuses = {run["status"] for run in runs}
            if statuses & self._RUN_IN_FLIGHT:
                reason = "process restarted with an in-flight workflow node job"
                in_flight_runs = [
                    run for run in runs if run["status"] in self._RUN_IN_FLIGHT
                ]
                for run in in_flight_runs:
                    conn.execute(
                        """
                        UPDATE workflow_node_job
                        SET status = 'execution_unknown', uncertainty_reason = ?,
                            update_time = ?
                        WHERE uuid = ? AND deleted_at IS NULL
                          AND status IN ('dispatched', 'running', 'cancel_requested')
                        """,
                        (reason, now, run["current_job_uuid"]),
                    )
                    self._sync_run_projection(conn, run["uuid"], now)
                from .resource_intervals import ResourceIntervals

                ResourceIntervals.settle_jobs(conn, [run["current_job_uuid"] for run in in_flight_runs])
                # 只可能从 active 进入；记住恢复目标，裁决完成后回到它
                conn.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = 'waiting_reconciliation',
                        reconciliation_resume_control_status = COALESCE(
                            reconciliation_resume_control_status, 'active'
                        ),
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
            elif not runs or statuses <= {"succeeded", "skipped"}:
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
                    SET status = CASE WHEN status = 'canceling' THEN status ELSE 'running' END,
                        started_at = COALESCE(started_at, ?),
                        update_time = ?
                    WHERE uuid = ?
                    """,
                    (now, now, task_uuid),
                )
                state = "ready"
        return {
            "state": state,
            "task": self.get_task(task_uuid),
            "runs": self.list_node_runs(task_uuid, include_attempts=False),
        }

    def settle_task_reconciliation(self, task_uuid: str) -> Dict[str, Any]:
        """重启后需要裁决的 attempt 全部收敛时，把任务从 ``waiting_reconciliation`` 恢复。

        需要裁决的是在飞 attempt 转成的 execution_unknown，以及失败后等待决策、但执行面
        的决策上下文已随旧进程丢失的 intervention_required；仍有节点运行处于这两种状态
        时不改任何状态，任务不在 waiting_reconciliation 时也是 no-op。恢复目标取进入时
        记下的 ``reconciliation_resume_control_status``（缺省 active）。
        """

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT status, control_status, reconciliation_resume_control_status
                FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL
                """,
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            if row["control_status"] == "waiting_reconciliation":
                pending = conn.execute(
                    """
                    SELECT COUNT(*) FROM workflow_node_run
                    WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                      AND status IN ('execution_unknown', 'intervention_required')
                    """,
                    (task_uuid,),
                ).fetchone()[0]
                if int(pending) == 0:
                    resume = row["reconciliation_resume_control_status"] or "active"
                    conn.execute(
                        """
                        UPDATE workflow_task
                        SET control_status = ?, reconciliation_resume_control_status = NULL,
                            attention_reason = NULL, update_time = ?
                        WHERE uuid = ?
                        """,
                        (resume, now, task_uuid),
                    )
                    self._append_event(
                        conn,
                        event="workflow.task.changed",
                        data={
                            "workflow_task_uuid": task_uuid,
                            "status": row["status"],
                            "control_status": resume,
                        },
                        now=now,
                    )
        return self.get_task(task_uuid)

    def mark_job_running(self, job_uuid: str) -> Dict[str, Any]:
        """在发送设备副作用前持久化 attempt running，并投影到节点运行。"""

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if row["status"] in self._RUN_TERMINAL:
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
            self._emit_job_changed(conn, row, "running", now)
            self._sync_run_projection(conn, row["workflow_node_run_uuid"], now)
        return self.get_job(job_uuid)

    def mark_job_decision_pending(
        self, job_uuid: str, report: Dict[str, Any]
    ) -> Dict[str, Any]:
        """失败 attempt 被执行面挂起等待决策：attempt 与节点运行进入 ``intervention_required``。

        决策报告摘要写入 ``control_data.pending_decision``，画布据此展示"待决策"并
        跳转到决策链；放行后由 ``record_job_terminal`` 收敛。重启后处于
        ``execution_unknown`` 的 attempt 开裁决时保持该状态——它表达的是"执行结果
        未知"而不是"已知失败"，前端据此区分文案。
        """

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if row["status"] in self._RUN_TERMINAL:
                return self._job_row(row)
            status = (
                "execution_unknown"
                if row["status"] == "execution_unknown"
                else "intervention_required"
            )
            control_data = _load(row["control_data"], {})
            control_data["pending_decision"] = {
                "decision_id": report.get("decision_id"),
                "exception_type": report.get("exception_type"),
                "error_message": report.get("error_message"),
                "options": [
                    str(option.get("action"))
                    for option in report.get("options") or []
                    if isinstance(option, dict)
                ],
                "retry_count": report.get("retry_count"),
                "max_retries": report.get("max_retries"),
                "expires_at": report.get("expires_at"),
            }
            conn.execute(
                """
                UPDATE workflow_node_job
                SET status=?, control_data=?, update_time=?
                WHERE uuid=?
                """,
                (status, _json(control_data), now, job_uuid),
            )
            self._emit_job_changed(
                conn,
                row,
                status,
                now,
                decision_id=report.get("decision_id"),
            )
            self._sync_run_projection(conn, row["workflow_node_run_uuid"], now)
            # V1 工作流干预：同一事务持久化决策报告（/workflow-interventions）
            from .interventions import open_for_decision

            open_for_decision(conn, row, report, now)
        return self.get_job(job_uuid)

    def set_node_run_execution_timeout(self, run_uuid: str, seconds: int) -> Dict[str, Any]:
        """写回调度器派发前解析出的业务软超时（秒，向上取整）；0 表示未声明。"""

        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise StoreConflict("execution_timeout_seconds must be a non-negative integer")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_run WHERE uuid=? AND deleted_at IS NULL",
                (run_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node run {run_uuid} not found")
            if int(row["execution_timeout_seconds"] or 0) != seconds:
                conn.execute(
                    "UPDATE workflow_node_run SET execution_timeout_seconds=?, update_time=? WHERE uuid=?",
                    (seconds, now, run_uuid),
                )
        return self.get_node_run(run_uuid)

    def mark_job_decision_resumed(
        self, job_uuid: str, decision_id: str = ""
    ) -> Dict[str, Any]:
        """软超时（``execution_timeout``）决策收回：attempt 与节点运行从 ``intervention_required``
        回到 ``running``，``control_data.pending_decision`` 移入 ``resumed_decisions`` 留痕。

        动作从未停止过，所以不产生新 attempt、不改结果；``execution_unknown`` 与终态
        attempt 不受影响（幂等返回）。
        """

        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if row["status"] != "intervention_required":
                return self._job_row(row)
            control_data = _load(row["control_data"], {})
            pending = control_data.pop("pending_decision", None)
            if pending is not None:
                history = control_data.get("resumed_decisions")
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        **pending,
                        "resumed_at": now,
                        "resumed_decision_id": decision_id or pending.get("decision_id"),
                    }
                )
                control_data["resumed_decisions"] = history
            conn.execute(
                """
                UPDATE workflow_node_job
                SET status='running', control_data=?, update_time=?
                WHERE uuid=?
                """,
                (_json(control_data), now, job_uuid),
            )
            self._emit_job_changed(
                conn,
                row,
                "running",
                now,
                decision_id=decision_id or (pending or {}).get("decision_id"),
                resumed=True,
            )
            self._sync_run_projection(conn, row["workflow_node_run_uuid"], now)
            from .interventions import supersede_for_job

            supersede_for_job(conn, job_uuid, now)
        return self.get_job(job_uuid)

    def record_job_terminal(
        self,
        job_uuid: str,
        *,
        status: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
        error_resolution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """持久化 attempt 终态并投影到节点运行；``retry`` 决策在同一事务里追加新 attempt。

        由终态派生的占用收敛（执行 Claim、使用权与准入、区间、设备托管）在**同一事务**
        里提交，观察者不会读到"终态已落、托管还没跟上"的中间态。

        返回 ``{"job", "run", "next_job"}``：``next_job`` 非空表示节点运行没有终结，
        调度器应以它为新的执行器 job 重新申请资源并下发；否则以 ``run["status"]``
        收敛 DAG 节点。已终态的 attempt 幂等返回，不重复投影。
        """

        if status not in self._RUN_TERMINAL:
            raise ValueError(f"invalid terminal workflow node job status: {status}")
        resolution = dict(error_resolution or {})
        retry_requested = (
            status == "failed" and str(resolution.get("selected_action") or "") == "retry"
        )
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            run_uuid = row["workflow_node_run_uuid"]
            if row["status"] in self._RUN_TERMINAL:
                run = self.get_node_run(run_uuid, include_attempts=False)
                return {"job": self._job_row(row), "run": run, "next_job": None}
            conn.execute(
                """
                UPDATE workflow_node_job
                SET status=?, return_info=?, error_info=?, error_resolution=?,
                    finished_at=?, update_time=?
                WHERE uuid=?
                """,
                (
                    status,
                    _json(return_info),
                    _json(error_info),
                    _json(resolution),
                    now,
                    now,
                    job_uuid,
                ),
            )
            self._emit_job_changed(conn, row, status, now)
            self._finish_task_step(conn, row, now)
            from .interventions import supersede_for_job

            supersede_for_job(conn, job_uuid, now)
            next_job_uuid: Optional[str] = None
            if retry_requested:
                # 失败 attempt 保留为事实；同一节点运行追加下一 attempt 并切换 current，
                # 节点运行回到 pending 而不是 failed——任务不中断。
                next_job_uuid = str(uuid4())
                self._insert_attempt(
                    conn,
                    job_uuid=next_job_uuid,
                    run_uuid=run_uuid,
                    task_uuid=row["workflow_task_uuid"],
                    node_uuid=row["workflow_node_uuid"],
                    attempt_no=int(row["attempt_no"]) + 1,
                    retry_of_job_uuid=job_uuid,
                    trigger="retry_decision",
                    param=_load(row["param"], {}),
                    now=now,
                )
                conn.execute(
                    """
                    UPDATE workflow_node_run
                    SET current_job_uuid=?, attempt_count=attempt_count + 1, update_time=?
                    WHERE uuid=?
                    """,
                    (next_job_uuid, now, run_uuid),
                )
            run = self._sync_run_projection(conn, run_uuid, now)
            from .resource_intervals import ResourceIntervals

            ResourceIntervals.settle_jobs(conn, [job_uuid])
            job = self._job_row(
                conn.execute(
                    "SELECT * FROM workflow_node_job WHERE uuid=?", (job_uuid,)
                ).fetchone()
            )
            next_job = (
                self._job_row(
                    conn.execute(
                        "SELECT * FROM workflow_node_job WHERE uuid=?", (next_job_uuid,)
                    ).fetchone()
                )
                if next_job_uuid is not None
                else None
            )
        return {"job": job, "run": run, "next_job": next_job}

    def begin_loop_iteration(
        self,
        loop_run_uuid: str,
        *,
        iteration: int,
        progress: Dict[str, Any],
        body_run_uuids: Sequence[str],
    ) -> Dict[str, Any]:
        """开始循环的第 ``iteration``（0 起）轮：循环体节点运行各追加一个新 attempt。

        循环体节点上一轮的 attempt 保留为事实（前端按 attempt 历史看每一轮），节点运行
        切回 pending 等待本轮派发——与 retry 追加 attempt 同构，trigger 记 ``loop_iteration``。
        首轮（attempt 仍是 pending 的 initial）不追加。循环节点自身的 ``control_data.loop``
        记录当前轮次，``return_info.return_value`` 同步给出迭代变量，供下游/前端读取。

        返回 ``{"run": 循环节点运行, "next_jobs": {body_run_uuid: 新 attempt 行}}``。
        """

        now = utc_now()
        next_jobs: Dict[str, Dict[str, Any]] = {}
        with self.transaction() as conn:
            loop_row = conn.execute(
                "SELECT * FROM workflow_node_run WHERE uuid=? AND deleted_at IS NULL",
                (loop_run_uuid,),
            ).fetchone()
            if loop_row is None:
                raise StoreNotFound(f"workflow node run {loop_run_uuid} not found")
            for run_uuid in body_run_uuids:
                row = conn.execute(
                    "SELECT * FROM workflow_node_run WHERE uuid=? AND deleted_at IS NULL",
                    (run_uuid,),
                ).fetchone()
                if row is None:
                    raise StoreNotFound(f"workflow node run {run_uuid} not found")
                current = conn.execute(
                    "SELECT * FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                    (row["current_job_uuid"],),
                ).fetchone()
                if current is None:
                    raise StoreConflict(f"workflow node run {run_uuid} has no current attempt")
                if current["status"] == "pending" and current["started_at"] is None:
                    # 尚未用过的 attempt（首轮 / 上一轮没轮到它）直接复用
                    continue
                if current["status"] not in self._RUN_TERMINAL:
                    raise StoreConflict(
                        f"workflow node run {run_uuid} still has an in-flight attempt"
                    )
                next_job_uuid = str(uuid4())
                self._insert_attempt(
                    conn,
                    job_uuid=next_job_uuid,
                    run_uuid=run_uuid,
                    task_uuid=row["workflow_task_uuid"],
                    node_uuid=row["workflow_node_uuid"],
                    attempt_no=int(current["attempt_no"]) + 1,
                    retry_of_job_uuid=None,
                    trigger="loop_iteration",
                    param=_load(row["param"], {}),
                    now=now,
                )
                # control_data 一并清空：嵌套循环上一轮留下的 loop.iteration 不能被当成恢复点
                conn.execute(
                    """
                    UPDATE workflow_node_run
                    SET current_job_uuid=?, attempt_count=attempt_count + 1,
                        control_data='{}', update_time=?
                    WHERE uuid=?
                    """,
                    (next_job_uuid, now, run_uuid),
                )
                self._sync_run_projection(conn, run_uuid, now)
                next_jobs[run_uuid] = self._job_row(
                    conn.execute(
                        "SELECT * FROM workflow_node_job WHERE uuid=?", (next_job_uuid,)
                    ).fetchone()
                )
            control_data = _load(loop_row["control_data"], {})
            control_data["loop"] = {**dict(progress), "iteration": int(iteration)}
            return_info = {
                "suc": None,
                "suc_type": "loop",
                "return_value": {
                    "index": int(iteration),
                    "iteration": int(iteration) + 1,
                    "count": progress.get("count"),
                },
            }
            conn.execute(
                """
                UPDATE workflow_node_run
                SET control_data=?, return_info=?, update_time=?
                WHERE uuid=?
                """,
                (_json(control_data), _json(return_info), now, loop_run_uuid),
            )
            conn.execute(
                """
                UPDATE workflow_node_job
                SET control_data=?, return_info=?, update_time=?
                WHERE uuid=?
                """,
                (
                    _json({"loop": control_data["loop"]}),
                    _json(return_info),
                    now,
                    loop_row["current_job_uuid"],
                ),
            )
            self._append_event(
                conn,
                event="workflow.node_run.changed",
                data={
                    "workflow_node_run_uuid": loop_run_uuid,
                    "workflow_task_uuid": loop_row["workflow_task_uuid"],
                    "workflow_node_uuid": loop_row["workflow_node_uuid"],
                    "status": loop_row["status"],
                    "current_job_uuid": loop_row["current_job_uuid"],
                    "attempt_count": int(loop_row["attempt_count"]),
                    "loop": control_data["loop"],
                },
                now=now,
            )
            run = self._run_row(
                conn.execute(
                    "SELECT * FROM workflow_node_run WHERE uuid=?", (loop_run_uuid,)
                ).fetchone()
            )
        return {"run": run, "next_jobs": next_jobs}

    def close_node_run(self, run_uuid: str, *, status: str) -> Dict[str, Any]:
        """DAG 收敛（取消/上游失败）时给尚未终结的节点运行记终态：作用于当前 attempt。"""

        if status not in self._RUN_TERMINAL:
            raise ValueError(f"invalid terminal workflow node run status: {status}")
        with self.write_lock:
            current = self.get_node_run(run_uuid, include_attempts=False)
            if current["status"] in self._RUN_TERMINAL:
                return current
            if current["executor_kind"] not in {"loop", "condition"} and current["status"] in self._RUN_IN_FLIGHT:
                raise StoreConflict("在飞或待核实动作不能由 DAG 清理关闭，必须等待执行结果或人工核实")
            outcome = self.record_job_terminal(
                str(current["current_job_uuid"]),
                status=status,
                return_info={},
                error_info=([] if status == "succeeded" else [{"code": status}]),
            )
            return outcome["run"]

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
            unresolved = conn.execute(
                "SELECT 1 FROM workflow_node_run WHERE workflow_task_uuid=? AND deleted_at IS NULL "
                "AND executor_kind NOT IN ('loop','condition') "
                "AND status IN ('dispatched','running','cancel_requested','execution_unknown','intervention_required') LIMIT 1",
                (task_uuid,),
            ).fetchone()
            if unresolved:
                raise StoreConflict("任务仍有在飞或待核实动作，不能提前结束任务")
            from .device_tenancy import DeviceTenancies
            DeviceTenancies.reconcile(conn, task_uuid)
            if DeviceTenancies.snapshot(conn, task_uuid):
                if status == "succeeded":
                    status = "failed"
                error_info = [*error_info, {"code": "device_tenancy_active", "message": "任务仍托管样品，请核实并取走后释放设备"}]
                conn.execute("UPDATE workflow_task SET cleanup_status='requires_attention',attention_reason='device_tenancy_active' WHERE uuid=?", (task_uuid,))
            conn.execute(
                """
                UPDATE workflow_task
                SET status=?, output=?, error_info=?, finished_at=?, update_time=?
                WHERE uuid=?
                """,
                (status, _json(output), _json(error_info), now, now, task_uuid),
            )
            # 空循环、取消或计划失败可能没有叶动作消费许可，任务终结时收拢命令。
            conn.execute(
                "UPDATE workflow_task_command SET status='succeeded', consumed_at=?, update_time=? "
                "WHERE workflow_task_uuid=? AND status='pending' AND deleted_at IS NULL",
                (now, now, task_uuid),
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
                WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
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
            WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
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
            workflow = WorkflowStore.get_workflow(self, workflow_uuid, conn=conn)
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

    def read_event_window(
        self,
        *,
        after_id: int,
        limit: int,
    ) -> tuple[int, int, List[Dict[str, Any]]]:
        """在同一条 SQL 快照内读取最早 / 最新保留序号与事件页。

        清理可能在两次查询之间越过慢连接的游标；边界与事件页同一快照读取，
        SSE 才能可靠判断 ``cursor_expired`` / ``cursor_ahead``。空表两端都为 0。
        """

        with self._lock:
            rows = self._conn.execute(
                """
                WITH bounds AS (
                    SELECT
                        COALESCE((SELECT MIN(sequence) FROM frontend_event), 0) AS window_oldest,
                        COALESCE((SELECT MAX(sequence) FROM frontend_event), 0) AS window_latest
                ), page AS (
                    SELECT * FROM frontend_event WHERE sequence > ? ORDER BY sequence LIMIT ?
                )
                SELECT bounds.window_oldest, bounds.window_latest, page.*
                FROM bounds LEFT JOIN page ON 1 = 1
                ORDER BY page.sequence
                """,
                (after_id, limit),
            ).fetchall()
        events = [
            {
                "id": row["sequence"],
                "uuid": row["uuid"],
                "event": row["type"],
                "aggregate_uuid": row["aggregate_uuid"],
                "data": _load(row["payload"], {}),
                "create_time": row["create_time"],
            }
            for row in rows
            if row["sequence"] is not None
        ]
        return int(rows[0]["window_oldest"]), int(rows[0]["window_latest"]), events

    def prune_frontend_events(
        self,
        *,
        before: str,
        max_count: int,
        batch_size: int,
    ) -> int:
        """在一个有界事务里删除超龄或超量的连续前缀，返回删除条数。

        始终保留最新一条作为持久水位，空闲库的水位不会回退；遇到仍需保留的
        通知就停止，不在序号中间挖洞。只处理通知表，不动任务、Job 或审计记录。
        """

        if max_count < 1 or batch_size < 1:
            raise ValueError("事件保留数量和清理批量必须为正整数")
        with self.transaction() as conn:
            latest = int(
                conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM frontend_event").fetchone()[0]
            )
            floor_row = conn.execute(
                "SELECT sequence FROM frontend_event ORDER BY sequence DESC LIMIT 1 OFFSET ?",
                (max_count - 1,),
            ).fetchone()
            count_floor = int(floor_row[0]) if floor_row else 0
            rows = conn.execute(
                "SELECT sequence, create_time FROM frontend_event ORDER BY sequence LIMIT ?",
                (batch_size,),
            ).fetchall()
            through = 0
            for row in rows:
                if row["sequence"] >= latest:
                    break
                if row["sequence"] >= count_floor and row["create_time"] >= before:
                    break
                through = row["sequence"]
            if not through:
                return 0
            return conn.execute(
                "DELETE FROM frontend_event WHERE sequence <= ?",
                (through,),
            ).rowcount

    def latest_event_sequence(self) -> int:
        """当前最新的持久通知序号，空库为 0。

        SSE 首连在发送响应头之前用它固定水位；每次轮询也用它识别超前游标。
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM frontend_event"
            ).fetchone()
        return int(row[0])

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
            "workflow_node_run",
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
        # V1：人工确认配置只在非空时输出
        manual_confirmation = _load(row["manual_confirmation"], {}) if "manual_confirmation" in row.keys() else {}
        if manual_confirmation:
            result["manual_confirmation"] = manual_confirmation
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

    @staticmethod
    def _publication_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "uuid": row["uuid"],
            "create_time": row["create_time"],
            "workflow_uuid": row["workflow_uuid"],
            "revision": row["revision"],
            "name": row["name"],
            "source_hash": row["source_hash"],
            "contract_digest": row["contract_digest"],
            "snapshot": _load(row["snapshot"], {}),
        }

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
            "idempotency_key",
        )
        # ad_hoc 幂等复用需要指纹比对；workflow 任务恒为空串，不额外暴露
        fingerprint = row["request_fingerprint"]
        if fingerprint:
            result["request_fingerprint"] = fingerprint
        # V1 字段：run_mode 是 dev 的当前模式，V1 的 execution_mode 与之同义；单步模式恒关闸。
        execution_mode = "step" if row["run_mode"] == "step" else "normal"
        result["priority"] = row["priority"]
        result["inventory_bindings"] = _load(row["inventory_bindings"], [])
        result["execution_mode"] = execution_mode
        result["dispatch_gate"] = "paused" if execution_mode == "step" else row["dispatch_gate"]
        result["wait_reason"] = {}
        return result

    @classmethod
    def _run_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_uuid": row["workflow_node_uuid"],
            "topological_index": row["topological_index"],
            "executor_kind": row["executor_kind"],
            "execution_policy": _load(row["execution_policy"], {}),
            "execution_timeout_seconds": row["execution_timeout_seconds"],
            "param": _load(row["param"], {}),
            "status": row["status"],
            "current_job_uuid": row["current_job_uuid"],
            "attempt_count": row["attempt_count"],
            "return_info": _load(row["return_info"], {}),
            "error_info": _load(row["error_info"], []),
            "feedback_data": _load(row["feedback_data"], {}),
            "control_data": _load(row["control_data"], {}),
        }
        cls._add_optional(result, row, "material_uuid", "started_at", "finished_at")
        return result

    @classmethod
    def _job_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            **cls._base(row),
            "workflow_node_run_uuid": row["workflow_node_run_uuid"],
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_uuid": row["workflow_node_uuid"],
            "attempt_no": row["attempt_no"],
            "trigger": row["trigger"],
            "feedback_sequence": row["feedback_sequence"],
            "status": row["status"],
            "param": _load(row["param"], {}),
            "feedback_data": _load(row["feedback_data"], {}),
            "return_info": _load(row["return_info"], {}),
            "error_resolution": _load(row["error_resolution"], {}),
            "control_data": _load(row["control_data"], {}),
            "error_info": _load(row["error_info"], []),
        }
        cls._add_optional(
            result,
            row,
            "retry_of_job_uuid",
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
    "WorkflowStore",
    "utc_now",
]
