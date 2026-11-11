"""Registry Authority：条目级版本的上报替换、冲突挂起与还原。

Edge 通过 ``/resource-templates`` 上报完整模板快照，本服务按条目独立处理：

- 任何字段变化都生成一个不可变的新版本；
- 变化条目做表关系冲突检测：被 workflow 节点引用的 action 被删除或定义
  变化时挂起为 pending 版本，由 ``apply`` 确认或 ``dismiss`` 忽略；
  无冲突的版本直接生效；
- 上报集合里消失的条目软移除（版本历史保留，可 ``restore`` 复活）；
- 每次上报落一条批次统计（新增/更新/挂起/移除/复活/不变/不可用）；
- 为调度器提供 ``materials_need_lock`` 镜像查询（读生效版本）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid as uuid_module
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence

try:  # 200 个条目的 payload 合计十几 MB，orjson 解析比标准库快数倍
    import orjson as _orjson
except ImportError:  # pragma: no cover - orjson 是可选加速
    _orjson = None

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.runtime.registry import (
    RegistryAffectedNode,
    RegistryEntrySummary,
    RegistryPendingImpact,
    RegistryReportSummary,
)
from unilabos.server.database.sqlite_domain import DomainDatabase, SqliteDomain
from unilabos.server.database.tables.runtime import (
    RUNTIME_DATABASE,
    RegistryEntryRecord,
    RegistryEntryStateRecord,
    RegistryReportRecord,
)

logger = logging.getLogger(__name__)

#: uuid5 命名空间：注册表模板身份（固定值，跨进程/跨机器一致）。
REGISTRY_TEMPLATE_NAMESPACE = uuid_module.uuid5(
    uuid_module.NAMESPACE_URL, "unilabos://registry-template"
)

#: ``workflow``：设备包 ``@workflow`` 声明的工作流模板，与设备 / 资源同一套条目版本化。
_VALID_REGISTRY_TYPES = frozenset({"device", "resource", "workflow"})
#: ``loop``：循环容器（``with ctx.loop_for / ctx.loop_while``），循环体节点以 ``parent`` 指向它。
_WORKFLOW_TEMPLATE_NODE_KINDS = frozenset({"action", "slot", "template", "loop", "condition"})

#: 返回活跃 workflow 节点对模板 action 的引用明细行，字段见
#: ``WorkflowService.list_template_action_references``（template_uuid / action /
#: node_uuid / node_name / workflow_uuid / workflow_name）。
ReferenceRowsResolver = Callable[[], Sequence[Mapping[str, Any]]]


class RegistryAuthorityError(RuntimeError):
    """上报内容不合法或条目操作无法执行。"""


def template_uuid(name: str) -> str:
    """按模板 id 派生稳定 uuid，统一上报身份与 workflow 引用。"""

    return str(uuid_module.uuid5(REGISTRY_TEMPLATE_NAMESPACE, str(name)))


class RegistryService(SqliteDomain):
    """串行化 registry 三表读写，维护生效条目的内存索引。

    生产环境表落 ``runtime.db``：把 RuntimeService 实例作为 ``database``
    传入即共享其 connection 与 write_lock（同库单连接单写者）；测试可传
    路径独立建一个 runtime 库（同一 RUNTIME_DATABASE 规格）。
    """

    def __init__(
        self,
        database: DomainDatabase,
        *,
        reference_rows_resolver: Optional[ReferenceRowsResolver] = None,
    ) -> None:
        super().__init__(database, RUNTIME_DATABASE)
        self._resolver = reference_rows_resolver
        self._lock = threading.RLock()
        # 生效条目的 content_sha256 索引：上报比对只看哈希，不必为每个条目重算 payload 哈希
        self._active_hashes: Dict[str, str] = {}
        self._active: Dict[str, Dict[str, Any]] = self._load_active_payloads()

    # ── registry_entry（不可变版本行） ──────────────────────────

    @staticmethod
    def _entry(row: sqlite3.Row) -> RegistryEntryRecord:
        values = dict(row)
        values["payload"] = _load(values["payload"])
        return RegistryEntryRecord.model_validate(values)

    def _insert_entry(self, record: RegistryEntryRecord) -> None:
        values = record.model_dump(mode="python")
        with self.write():
            self.connection.execute(
                """
                INSERT INTO registry_entry(
                    name,version,created_at_ms,source,edge_uuid,
                    restored_from,content_sha256,payload
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    values["name"],
                    values["version"],
                    values["created_at_ms"],
                    values["source"],
                    values["edge_uuid"],
                    values["restored_from"],
                    values["content_sha256"],
                    _dump(values["payload"]),
                ),
            )

    def find_entry(self, name: str, version: int) -> Optional[RegistryEntryRecord]:
        with self.write_lock:
            row = self.connection.execute(
                "SELECT * FROM registry_entry WHERE name=? AND version=?",
                (name, version),
            ).fetchone()
            return self._entry(row) if row is not None else None

    def _max_entry_version(self, name: str) -> int:
        with self.write_lock:
            row = self.connection.execute(
                "SELECT MAX(version) FROM registry_entry WHERE name=?", (name,)
            ).fetchone()
            return int(row[0] or 0)

    def _list_entry_versions(self, name: str) -> list[RegistryEntryRecord]:
        with self.write_lock:
            rows = self.connection.execute(
                "SELECT * FROM registry_entry WHERE name=? ORDER BY version DESC",
                (name,),
            ).fetchall()
            return [self._entry(row) for row in rows]

    # ── registry_entry_state（每条目一行可变状态） ─────────────

    @staticmethod
    def _state(row: sqlite3.Row) -> RegistryEntryStateRecord:
        values = dict(row)
        values["pending_conflicts"] = _load(values["pending_conflicts"])
        return RegistryEntryStateRecord.model_validate(values)

    def _find_state(self, name: str) -> Optional[RegistryEntryStateRecord]:
        with self.write_lock:
            row = self.connection.execute(
                "SELECT * FROM registry_entry_state WHERE name=?", (name,)
            ).fetchone()
            return self._state(row) if row is not None else None

    def _upsert_state(self, record: RegistryEntryStateRecord) -> None:
        values = record.model_dump(mode="python")
        with self.write():
            self.connection.execute(
                """
                INSERT INTO registry_entry_state(
                    name,template_uuid,active_version,pending_version,
                    pending_conflicts,unusable_reason,removed_at_ms,updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    template_uuid=excluded.template_uuid,
                    active_version=excluded.active_version,
                    pending_version=excluded.pending_version,
                    pending_conflicts=excluded.pending_conflicts,
                    unusable_reason=excluded.unusable_reason,
                    removed_at_ms=excluded.removed_at_ms,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    values["name"],
                    values["template_uuid"],
                    values["active_version"],
                    values["pending_version"],
                    _dump(values["pending_conflicts"]),
                    values["unusable_reason"],
                    values["removed_at_ms"],
                    values["updated_at_ms"],
                ),
            )

    def _list_states(self) -> list[RegistryEntryStateRecord]:
        with self.write_lock:
            rows = self.connection.execute(
                "SELECT * FROM registry_entry_state ORDER BY name"
            ).fetchall()
            return [self._state(row) for row in rows]

    def _load_active_payloads(self) -> dict[str, dict[str, Any]]:
        """一次 JOIN 加载全部生效条目的 payload 与内容哈希（服务启动时重建索引）。"""

        with self.write_lock:
            rows = self.connection.execute(
                """
                SELECT s.name AS name, e.payload AS payload, e.content_sha256 AS content_sha256
                FROM registry_entry_state s
                JOIN registry_entry e
                  ON e.name = s.name AND e.version = s.active_version
                WHERE s.active_version IS NOT NULL AND s.removed_at_ms IS NULL
                """
            ).fetchall()
            self._active_hashes = {row["name"]: str(row["content_sha256"]) for row in rows}
            return {row["name"]: _load(row["payload"]) for row in rows}

    # ── registry_report（上报批次统计） ────────────────────────

    def _insert_report(self, record: RegistryReportRecord) -> int:
        values = record.model_dump(mode="python")
        with self.write():
            cursor = self.connection.execute(
                "INSERT INTO registry_report(created_at_ms,edge_uuid,summary) "
                "VALUES (?,?,?)",
                (
                    values["created_at_ms"],
                    values["edge_uuid"],
                    _dump(values["summary"]),
                ),
            )
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # 上报（条目级替换）
    # ------------------------------------------------------------------

    def digest(self) -> Dict[str, Dict[str, str]]:
        """权威当前持有的内容哈希：``{"active": {name: sha}, "pending": {name: sha}}``。

        Host 上报前先取这份索引，只为权威没有的哈希附完整定义。挂起版本也算持有：
        再报同一份挂起内容不必重传，也不再多生成一个版本。软移除条目的生效哈希也在
        ``active`` 里（再报同一哈希即复活），所以这里按状态表查而不只看活跃索引。
        """

        with self._lock:
            active: Dict[str, str] = {}
            pending: Dict[str, str] = {}
            for state in self._list_states():
                if state.active_version is not None:
                    sha = self._active_hashes.get(state.name)
                    if sha is None:
                        entry = self.find_entry(state.name, state.active_version)
                        sha = entry.content_sha256 if entry is not None else None
                    if sha is not None:
                        active[state.name] = sha
                if state.pending_version is not None:
                    entry = self.find_entry(state.name, state.pending_version)
                    if entry is not None:
                        pending[state.name] = entry.content_sha256
        return {"active": active, "pending": pending}

    def report(
        self,
        definitions: Sequence[Mapping[str, Any]],
        *,
        edge_uuid: str = "",
    ) -> Dict[str, Any]:
        """全量上报（旧形状：每个条目都是完整定义）。逐条目比对：变了就为该条目升版本；
        被 workflow 引用的 action 发生删除/变化时挂起为 pending，否则自动生效。"""

        entries, unusable = _normalize_definitions(definitions)
        return self._report(
            {name: (entry, None) for name, entry in entries.items()},
            unusable,
            edge_uuid=edge_uuid,
        )

    def report_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        edge_uuid: str = "",
    ) -> Dict[str, Any]:
        """按哈希增量的全量上报：``[{id, content_sha256, payload?}]``。

        带 payload 的条目与旧形状同样处理（哈希由权威自己重算）；只带哈希的条目必须命中
        权威已有的生效 / 挂起版本，否则进结果的 ``missing``，由 Host 补 payload 再报。
        """

        normalized: Dict[str, tuple[Optional[Dict[str, Any]], Optional[str]]] = {}
        unusable: List[Dict[str, str]] = []
        for raw in entries:
            if not isinstance(raw, Mapping):
                unusable.append({"id": "", "reason": "not-an-object"})
                continue
            name = str(raw.get("id") or "").strip()
            if not name:
                unusable.append({"id": "", "reason": "missing-id"})
                continue
            if name in normalized:
                unusable.append({"id": name, "reason": "duplicate-id"})
                continue
            payload = raw.get("payload")
            claimed = str(raw.get("content_sha256") or "").strip()
            if payload is None and not claimed:
                unusable.append({"id": name, "reason": "missing-payload-and-hash"})
                continue
            if payload is not None and not isinstance(payload, Mapping):
                unusable.append({"id": name, "reason": "not-an-object"})
                continue
            normalized[name] = (dict(payload) if payload is not None else None, claimed or None)
        return self._report(normalized, unusable, edge_uuid=edge_uuid)

    def _report(
        self,
        entries: Mapping[str, tuple[Optional[Dict[str, Any]], Optional[str]]],
        unusable: List[Dict[str, str]],
        *,
        edge_uuid: str,
    ) -> Dict[str, Any]:
        referenced = self._referenced_actions()
        now_ms = int(time.time() * 1000)

        detail: Dict[str, Any] = {
            "added": [],
            "updated": [],
            "pending": [],
            "unchanged": [],
            "removed": [],
            "revived": [],
            "unusable": unusable,
        }
        missing: List[str] = []

        with self._lock:
            for name, (payload, claimed_sha) in entries.items():
                if payload is not None:
                    self._report_entry(
                        name, payload, referenced, now_ms, edge_uuid, detail
                    )
                elif not self._report_hash_only(name, claimed_sha or "", now_ms, detail):
                    missing.append(name)

            # 上报集合外的既有条目转为软移除；非 active 条目不重复计数。
            # 只发哈希、尚待补 payload 的条目也在集合里，不会被误判为移除。
            reported_names = set(entries)
            for state in self._list_states():
                if state.name in reported_names:
                    continue
                if state.removed_at_ms is not None or state.active_version is None:
                    continue
                state.removed_at_ms = now_ms
                state.updated_at_ms = now_ms
                self._upsert_state(state)
                self._drop_active(state.name)
                detail["removed"].append(state.name)

            summary = _summarize(detail, total=len(entries))
            report_id = self._insert_report(
                RegistryReportRecord(
                    created_at_ms=now_ms,
                    edge_uuid=str(edge_uuid or ""),
                    summary=summary,
                )
            )

        return {
            "report_id": report_id,
            "created_at_ms": now_ms,
            "summary": summary,
            "templates": [
                {"name": name, "uuid": template_uuid(name)}
                for name in sorted(entries)
                if name not in missing
            ],
            "missing": missing,
        }

    def _report_hash_only(
        self, name: str, claimed_sha: str, now_ms: int, detail: Dict[str, Any]
    ) -> bool:
        """只带哈希的条目：命中生效版本按"未变"（软移除的则复活），命中挂起版本按"仍挂起"。

        Returns:
            False 表示权威没有这个哈希的版本，需要 Host 补完整 payload。
        """

        state = self._find_state(name)
        if state is None or not claimed_sha:
            return False
        active_sha = self._active_hashes.get(name)
        active_entry = None
        if state.active_version is not None and active_sha is None:
            # 软移除条目不在活跃索引中：从版本表读生效版本
            active_entry = self.find_entry(name, state.active_version)
            active_sha = active_entry.content_sha256 if active_entry is not None else None

        if state.active_version is not None and active_sha == claimed_sha:
            was_removed = state.removed_at_ms is not None
            changed = bool(state.unusable_reason) or was_removed
            state.unusable_reason = ""
            if was_removed:
                state.removed_at_ms = None
                detail["revived"].append(name)
                payload = active_entry.payload if active_entry is not None else None
                if payload is None:
                    stored = self.find_entry(name, state.active_version)
                    payload = stored.payload if stored is not None else {}
                self._set_active(name, payload, claimed_sha)
            else:
                detail["unchanged"].append(name)
            if changed:
                state.updated_at_ms = now_ms
                self._upsert_state(state)
            return True

        if state.pending_version is not None:
            pending = self.find_entry(name, state.pending_version)
            if pending is not None and pending.content_sha256 == claimed_sha:
                # 同一份挂起内容再报一次：不再生成新版本，等前端确认或忽略
                detail["pending"].append(
                    {"name": name, "conflicts": list(state.pending_conflicts)}
                )
                return True
        return False

    def _report_entry(
        self,
        name: str,
        entry: Dict[str, Any],
        referenced: Mapping[str, Collection[str]],
        now_ms: int,
        edge_uuid: str,
        detail: Dict[str, Any],
    ) -> None:
        state = self._find_state(name)
        usability = _usability_issue(entry)

        if usability:
            # 无效定义不进入版本历史；已有生效版本仍可继续服务。
            record = state or _new_state(name, now_ms)
            record.unusable_reason = usability
            record.updated_at_ms = now_ms
            self._upsert_state(record)
            detail["unusable"].append({"id": name, "reason": usability})
            return

        content_sha = canonical_hash(entry)
        active_payload = self._active.get(name)
        active_sha = self._active_hashes.get(name)
        was_removed = state is not None and state.removed_at_ms is not None

        if state is not None and state.active_version is not None and active_payload is None:
            # 软移除条目不在活跃索引中，需从版本表读取生效内容。
            stored = self.find_entry(name, state.active_version)
            if stored is not None:
                active_payload = stored.payload
                active_sha = stored.content_sha256

        if (
            state is not None
            and state.active_version is not None
            and active_payload is not None
            and active_sha == content_sha
        ):
            # 相同内容不会生成版本；重新上报可恢复软移除条目。
            changed = bool(state.unusable_reason) or was_removed
            state.unusable_reason = ""
            if was_removed:
                state.removed_at_ms = None
                detail["revived"].append(name)
                self._set_active(name, entry, content_sha)
            else:
                detail["unchanged"].append(name)
            if changed:
                state.updated_at_ms = now_ms
                self._upsert_state(state)
            return

        version = self._max_entry_version(name) + 1
        self._insert_entry(
            RegistryEntryRecord(
                name=name,
                version=version,
                created_at_ms=now_ms,
                source="edge-report",
                edge_uuid=str(edge_uuid or ""),
                content_sha256=content_sha,
                payload=entry,
            )
        )

        if state is None or state.active_version is None:
            # 首个有效版本直接生效。
            record = state or _new_state(name, now_ms)
            record.active_version = version
            record.pending_version = None
            record.pending_conflicts = []
            record.unusable_reason = ""
            record.removed_at_ms = None
            record.updated_at_ms = now_ms
            self._upsert_state(record)
            self._set_active(name, entry, content_sha)
            detail["added"].append(name)
            return

        conflicts = _detect_conflicts(
            active_payload or {},
            entry,
            referenced.get(state.template_uuid, ()),
        )
        state.unusable_reason = ""
        if was_removed:
            state.removed_at_ms = None
            detail["revived"].append(name)

        if conflicts:
            # 影响活跃 workflow 的 action 变更需要显式确认。
            state.pending_version = version
            state.pending_conflicts = conflicts
            detail["pending"].append({"name": name, "conflicts": conflicts})
        else:
            state.active_version = version
            state.pending_version = None
            state.pending_conflicts = []
            self._set_active(name, entry, content_sha)
            detail["updated"].append(name)
        state.updated_at_ms = now_ms
        self._upsert_state(state)

    def _set_active(self, name: str, payload: Mapping[str, Any], content_sha: str) -> None:
        self._active[name] = dict(payload)
        self._active_hashes[name] = content_sha

    def _drop_active(self, name: str) -> None:
        self._active.pop(name, None)
        self._active_hashes.pop(name, None)

    # ------------------------------------------------------------------
    # 前端操作：确认 / 忽略 / 还原
    # ------------------------------------------------------------------

    def apply_pending(self, name: str) -> Dict[str, Any]:
        """把挂起版本切换为生效版本。"""

        with self._lock:
            state = self._require_state(name)
            if state.pending_version is None:
                raise RegistryAuthorityError(f"entry {name} has no pending version")
            entry = self.find_entry(name, state.pending_version)
            if entry is None:
                raise RegistryAuthorityError(
                    f"entry {name} pending version {state.pending_version} missing"
                )
            state.active_version = state.pending_version
            state.pending_version = None
            state.pending_conflicts = []
            state.removed_at_ms = None
            state.updated_at_ms = int(time.time() * 1000)
            self._upsert_state(state)
            self._set_active(name, entry.payload, entry.content_sha256)
            return self._entry_summary(state)

    def dismiss_pending(self, name: str) -> Dict[str, Any]:
        """忽略挂起版本（版本行保留在历史里，生效版本不动）。"""

        with self._lock:
            state = self._require_state(name)
            if state.pending_version is None:
                raise RegistryAuthorityError(f"entry {name} has no pending version")
            state.pending_version = None
            state.pending_conflicts = []
            state.updated_at_ms = int(time.time() * 1000)
            self._upsert_state(state)
            return self._entry_summary(state)

    def restore(self, name: str, version: int) -> Dict[str, Any]:
        """把历史版本内容还原为新的生效版本（版本号继续自增）。

        显式人工操作：不走冲突挂起，同时清掉挂起版本、复活软移除条目。
        """

        with self._lock:
            snapshot = self.find_entry(name, int(version))
            if snapshot is None:
                raise RegistryAuthorityError(
                    f"entry {name} version {version} not found"
                )
            state = self._require_state(name)
            now_ms = int(time.time() * 1000)

            if (
                state.active_version is not None
                and state.removed_at_ms is None
                and name in self._active
                and self._active_hashes.get(name) == snapshot.content_sha256
            ):
                return self._entry_summary(state)

            new_version = self._max_entry_version(name) + 1
            self._insert_entry(
                RegistryEntryRecord(
                    name=name,
                    version=new_version,
                    created_at_ms=now_ms,
                    source="restore",
                    edge_uuid=snapshot.edge_uuid,
                    restored_from=snapshot.version,
                    content_sha256=snapshot.content_sha256,
                    payload=snapshot.payload,
                )
            )
            state.active_version = new_version
            state.pending_version = None
            state.pending_conflicts = []
            state.unusable_reason = ""
            state.removed_at_ms = None
            state.updated_at_ms = now_ms
            self._upsert_state(state)
            self._set_active(name, snapshot.payload, snapshot.content_sha256)
            return self._entry_summary(state)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_entries(self, *, status: str = "") -> List[Dict[str, Any]]:
        """条目状态列表。``status`` 过滤：active/pending/removed/unusable。"""

        with self._lock:
            summaries = [
                self._entry_summary(state) for state in self._list_states()
            ]
        if status:
            summaries = [s for s in summaries if status in s["status"]]
        return summaries

    def pending_impacts(self) -> List[Dict[str, Any]]:
        """挂起条目的影响面：冲突明细 + 受影响的 workflow 画布节点。

        前端据此标记需要确认更新的节点；只列引用了冲突 action
        的节点，同模板其他 action 的节点不受影响。
        """

        with self._lock:
            pending_states = [
                state
                for state in self._list_states()
                if state.pending_version is not None
            ]
        if not pending_states:
            return []
        rows = self._reference_rows()
        impacts: List[Dict[str, Any]] = []
        for state in pending_states:
            conflict_actions = {
                str(conflict.get("action") or "")
                for conflict in state.pending_conflicts
                if isinstance(conflict, Mapping)
            }
            affected = [
                RegistryAffectedNode(
                    workflow_uuid=str(row.get("workflow_uuid") or ""),
                    workflow_name=str(row.get("workflow_name") or ""),
                    node_uuid=str(row.get("node_uuid") or ""),
                    node_name=str(row.get("node_name") or ""),
                    action=str(row.get("action") or ""),
                )
                for row in rows
                if str(row.get("template_uuid") or "") == state.template_uuid
                and str(row.get("action") or "") in conflict_actions
            ]
            impacts.append(
                RegistryPendingImpact(
                    name=state.name,
                    template_uuid=state.template_uuid,
                    active_version=state.active_version,
                    pending_version=int(state.pending_version or 0),
                    conflicts=list(state.pending_conflicts),
                    affected_nodes=affected,
                ).model_dump(mode="python")
            )
        return impacts

    def entry_detail(self, name: str) -> Dict[str, Any]:
        with self._lock:
            state = self._require_state(name)
            summary = self._entry_summary(state)
            if state.active_version is not None:
                active = self.find_entry(name, state.active_version)
                summary["active_payload"] = active.payload if active else None
            if state.pending_version is not None:
                pending = self.find_entry(name, state.pending_version)
                summary["pending_payload"] = pending.payload if pending else None
            return summary

    def entry_versions(self, name: str) -> List[Dict[str, Any]]:
        records = self._list_entry_versions(name)
        if not records:
            raise RegistryAuthorityError(f"entry {name} not found")
        return [
            {
                "version": record.version,
                "created_at_ms": record.created_at_ms,
                "source": record.source,
                "edge_uuid": record.edge_uuid,
                "restored_from": record.restored_from,
                "content_sha256": record.content_sha256,
            }
            for record in records
        ]

    def get_entry_version(self, name: str, version: int) -> Dict[str, Any]:
        record = self.find_entry(name, int(version))
        if record is None:
            raise RegistryAuthorityError(f"entry {name} version {version} not found")
        return record.model_dump(mode="python")

    def list_reports(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[List[Dict[str, Any]], int]:
        with self.write_lock:
            total = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM registry_report"
                ).fetchone()[0]
            )
            rows = self.connection.execute(
                "SELECT * FROM registry_report "
                "ORDER BY report_id DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
        reports: List[Dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            values["summary"] = json.loads(values["summary"])
            reports.append(
                RegistryReportRecord.model_validate(values).model_dump(mode="python")
            )
        return reports, total

    def list_workflow_templates(self) -> List[Dict[str, Any]]:
        """生效的工作流模板条目（``registry_type=workflow``）payload，按显示名排序。

        前端"工作流模板"面板与 ``POST /workflows/from-template`` 的数据源；软移除 /
        挂起中的版本不在其中。
        """

        with self._lock:
            templates = [
                dict(payload)
                for payload in self._active.values()
                if str(payload.get("registry_type") or "") == "workflow"
            ]
        templates.sort(key=lambda item: (str(item.get("display_name") or ""), str(item.get("id"))))
        return templates

    def get_workflow_template(self, template_uuid: str) -> Optional[Dict[str, Any]]:
        """按模板 uuid 取生效模板；找不到（或已软移除）返回 None。"""

        wanted = str(template_uuid or "")
        for template in self.list_workflow_templates():
            if str(template.get("uuid") or "") == wanted:
                return template
        return None

    def action_definition(
        self, device_class: str, action_name: str
    ) -> Optional[Mapping[str, Any]]:
        """生效条目里某个动作的声明（``action_value_mappings`` 条目）。

        语义与 Edge 本地 Registry 查询对齐：动作名找不到时尝试 ``auto-`` 前缀。
        """

        with self._lock:
            entry = self._active.get(str(device_class or ""))
        if not entry:
            return None
        mappings = _action_definitions(entry)
        action = mappings.get(str(action_name or ""))
        if not isinstance(action, Mapping):
            action = mappings.get(f"auto-{action_name}")
        return action if isinstance(action, Mapping) else None

    def material_lock_parameters(self, device_class: str, action_name: str) -> List[str]:
        """从生效条目镜像解析动作的 ``materials_need_lock``。"""

        action = self.action_definition(device_class, action_name)
        if action is None:
            return []
        names = action.get("materials_need_lock")
        if not isinstance(names, (list, tuple)):
            return []
        return [str(item) for item in names if str(item).strip()]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _reference_rows(self) -> List[Mapping[str, Any]]:
        if self._resolver is None:
            return []
        try:
            return [row for row in self._resolver() if isinstance(row, Mapping)]
        except Exception:  # noqa: BLE001 - 引用查询失败不能阻断注册表刷新
            logger.exception("[Registry] workflow 引用查询失败，本次按无引用处理")
            return []

    def _referenced_actions(self) -> Mapping[str, Collection[str]]:
        referenced: Dict[str, set] = {}
        for row in self._reference_rows():
            referenced.setdefault(str(row.get("template_uuid") or ""), set()).add(
                str(row.get("action") or "")
            )
        return referenced

    def _require_state(self, name: str) -> RegistryEntryStateRecord:
        state = self._find_state(str(name or ""))
        if state is None:
            raise RegistryAuthorityError(f"entry {name} not found")
        return state

    @staticmethod
    def _entry_summary(state: RegistryEntryStateRecord) -> Dict[str, Any]:
        status: List[str] = []
        if state.removed_at_ms is not None:
            status.append("removed")
        elif state.active_version is not None:
            status.append("active")
        if state.pending_version is not None:
            status.append("pending")
        if state.unusable_reason:
            status.append("unusable")
        return RegistryEntrySummary(
            name=state.name,
            template_uuid=state.template_uuid,
            active_version=state.active_version,
            pending_version=state.pending_version,
            pending_conflicts=list(state.pending_conflicts),
            unusable_reason=state.unusable_reason,
            removed_at_ms=state.removed_at_ms,
            updated_at_ms=state.updated_at_ms,
            status=status,  # type: ignore[arg-type] - 上面只放合法标签
        ).model_dump(mode="python")


# ----------------------------------------------------------------------
# 纯函数：规范化 / 校验 / 冲突检测 / 统计
# ----------------------------------------------------------------------


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(text: Any) -> Any:
    if _orjson is not None:
        return _orjson.loads(text)
    return json.loads(text)


def _new_state(name: str, now_ms: int) -> RegistryEntryStateRecord:
    return RegistryEntryStateRecord(
        name=name,
        template_uuid=template_uuid(name),
        updated_at_ms=now_ms,
    )


def _normalize_definitions(
    definitions: Sequence[Mapping[str, Any]],
) -> tuple[Dict[str, Dict[str, Any]], List[Dict[str, str]]]:
    """按条目 id 规范化；id 缺失/重复的条目直接进不可用明细。"""

    if not isinstance(definitions, Sequence) or isinstance(definitions, (str, bytes)):
        raise RegistryAuthorityError("definitions must be a list of template objects")

    entries: Dict[str, Dict[str, Any]] = {}
    unusable: List[Dict[str, str]] = []
    for raw in definitions:
        if not isinstance(raw, Mapping):
            unusable.append({"id": "", "reason": "not-an-object"})
            continue
        entry = dict(raw)
        name = str(entry.get("id") or "").strip()
        if not name:
            unusable.append({"id": "", "reason": "missing-id"})
            continue
        if name in entries:
            unusable.append({"id": name, "reason": "duplicate-id"})
            continue
        entries[name] = entry
    return entries, unusable


def _usability_issue(entry: Mapping[str, Any]) -> str:
    registry_type = str(entry.get("registry_type") or "").strip()
    if registry_type not in _VALID_REGISTRY_TYPES:
        return "invalid-registry-type"
    if registry_type == "workflow":
        return _workflow_template_issue(entry)
    klass = entry.get("class")
    if not isinstance(klass, Mapping):
        return "missing-class"
    if registry_type == "device" and not str(klass.get("module") or "").strip():
        return "missing-class-module"
    handles = entry.get("handles")
    if handles is not None and not isinstance(handles, list):
        return "invalid-handles"
    if isinstance(handles, list) and any(
        not isinstance(handle, Mapping) for handle in handles
    ):
        return "invalid-handles"
    return ""


def _workflow_template_issue(entry: Mapping[str, Any]) -> str:
    """工作流模板条目形状：稳定 uuid + 显示名 + 角色 / 节点 / 边（前端模板同形）。"""

    try:
        uuid_module.UUID(str(entry.get("uuid") or ""))
    except ValueError:
        return "missing-template-uuid"
    if not str(entry.get("display_name") or "").strip():
        return "missing-display-name"
    nodes = entry.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return "missing-nodes"
    keys: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            return "invalid-nodes"
        key = str(node.get("key") or "")
        if not key or key in keys:
            return "invalid-node-key"
        keys.add(key)
        if str(node.get("kind") or "") not in _WORKFLOW_TEMPLATE_NODE_KINDS:
            return "invalid-node-kind"
        if node.get("kind") == "action" and not (
            str(node.get("role") or "") and str(node.get("action_name") or "")
        ):
            return "invalid-action-node"
        if node.get("kind") == "loop" and not isinstance(node.get("param"), Mapping):
            return "invalid-loop-node"
        if "description" in node and not isinstance(node["description"], str):
            return "invalid-node-description"
    # 循环体成员的 parent 必须指向本模板里的 loop 节点
    kinds_by_key = {str(node.get("key")): str(node.get("kind") or "") for node in nodes}
    if any(node.get("kind") == "condition" or node.get("condition_guards") for node in nodes):
        from unilabos.protocol.runtime.condition import remap_expression, validate_condition_graph

        identities = {key: str(uuid_module.uuid5(uuid_module.NAMESPACE_URL, key)) for key in keys}
        try:
            bound = {}
            for node in nodes:
                identity = identities[node["key"]]
                param = dict(node.get("param") or {})
                if node.get("kind") == "condition":
                    param = remap_expression(param, identities.__getitem__, reference_key="node_key")
                bound[identity] = {
                    "param": param,
                    "execution_policy": {"condition_guards": [
                        {"node_uuid": identities[guard["node_key"]], "equals": guard["equals"]}
                        for guard in node.get("condition_guards", [])
                    ]},
                    "meta_data": {"inventory_requirements": node.get("inventory_requirements")},
                }
            validate_condition_graph(
                bound, {identities[key]: kind for key, kind in kinds_by_key.items()},
                {identities[node["key"]]: identities[node["parent"]] if node.get("parent") else None for node in nodes},
                {key: node["param"] for key, node in bound.items()},
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            return "invalid-condition-node"
    for node in nodes:
        parent = node.get("parent")
        if parent is None:
            continue
        if kinds_by_key.get(str(parent)) != "loop" or str(parent) == str(node.get("key")):
            return "invalid-node-parent"
    if entry.get("guide") is not None and not _valid_workflow_guide(entry["guide"]):
        return "invalid-guide"
    roles = entry.get("roles")
    if not isinstance(roles, list) or any(
        not isinstance(role, Mapping) or not str(role.get("role") or "") for role in roles
    ):
        return "invalid-roles"
    role_ids = {str(role["role"]) for role in roles}
    if any(
        node.get("kind") == "action" and str(node.get("role")) not in role_ids
        for node in nodes
    ):
        return "unknown-node-role"
    edges = entry.get("edges")
    if not isinstance(edges, list) or any(
        not isinstance(edge, Mapping)
        or str(edge.get("source") or "") not in keys
        or str(edge.get("target") or "") not in keys
        for edge in edges
    ):
        return "invalid-edges"
    return ""


_WORKFLOW_GUIDE_SECTIONS = ("preparation", "expected", "notes")


def _valid_workflow_guide(guide: Any) -> bool:
    """操作指引：三段可选的字符串列表（运行前准备 / 预期效果 / 注意事项）。"""

    if not isinstance(guide, Mapping) or set(guide) - set(_WORKFLOW_GUIDE_SECTIONS):
        return False
    return all(
        isinstance(guide.get(section, []), list)
        and all(isinstance(item, str) for item in guide.get(section, []))
        for section in _WORKFLOW_GUIDE_SECTIONS
    )


def _action_definitions(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    klass = entry.get("class")
    if not isinstance(klass, Mapping):
        return {}
    mappings = klass.get("action_value_mappings")
    return mappings if isinstance(mappings, Mapping) else {}


def _detect_conflicts(
    old_entry: Mapping[str, Any],
    new_entry: Mapping[str, Any],
    referenced: Collection[str],
) -> List[Dict[str, str]]:
    """检测候选版本对活跃 workflow 所引用 action 的破坏性变更。

    生效版本中已经不存在的引用不属于候选版本引入的冲突。
    """

    if not referenced:
        return []
    old_actions = _action_definitions(old_entry)
    new_actions = _action_definitions(new_entry)
    conflicts: List[Dict[str, str]] = []
    for action in sorted({str(item) for item in referenced}):
        old_def = old_actions.get(action)
        new_def = new_actions.get(action)
        if old_def is None:
            continue
        if new_def is None:
            conflicts.append({"action": action, "reason": "action-removed"})
        elif canonical_hash(old_def) != canonical_hash(new_def):
            conflicts.append({"action": action, "reason": "action-changed"})
    return conflicts


def _summarize(detail: Mapping[str, Any], *, total: int) -> Dict[str, Any]:
    return RegistryReportSummary(
        counts={
            "total": total,
            "added": len(detail.get("added", [])),
            "updated": len(detail.get("updated", [])),
            "pending": len(detail.get("pending", [])),
            "unchanged": len(detail.get("unchanged", [])),
            "removed": len(detail.get("removed", [])),
            "revived": len(detail.get("revived", [])),
            "unusable": len(detail.get("unusable", [])),
        },
        added=list(detail.get("added", [])),
        updated=list(detail.get("updated", [])),
        pending=list(detail.get("pending", [])),
        removed=list(detail.get("removed", [])),
        revived=list(detail.get("revived", [])),
        unusable=list(detail.get("unusable", [])),
    ).model_dump(mode="python")


# ----------------------------------------------------------------------
# 进程级单例（backend 进程装配，API 层查找）
# ----------------------------------------------------------------------

_registry_service: Optional[RegistryService] = None


def set_registry_service(service: Optional[RegistryService]) -> None:
    global _registry_service
    _registry_service = service


def get_registry_service() -> Optional[RegistryService]:
    return _registry_service


__all__ = [
    "REGISTRY_TEMPLATE_NAMESPACE",
    "ReferenceRowsResolver",
    "RegistryAuthorityError",
    "RegistryService",
    "get_registry_service",
    "set_registry_service",
    "template_uuid",
]
