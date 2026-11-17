"""Registry Authority：条目级版本的上报替换、接受规则、挂起确认与还原。

权威自己不扫描设备 / 资源定义：它的注册表目录（工作流编译用的设备目录、节点模板、动作
Schema、工作流模板）只来自 Host 经 ``/registry/reports`` 的上报（单进程时进程内直接写）。
本服务按条目独立处理：

- 任何字段变化都生成一个不可变的新版本（同一内容的挂起 / 被忽略版本复用原版本号）；
- 新增或变化的条目先挂起为 pending 版本，命中自动接受规则才直接生效：

  - **规则 B（单机部署）**：上报的 Host 与权威在同一台机器（上报带的 ``machine_id`` 等于权威的，
    或单进程时进程内上报），并且权威没有记录到任何跨机器 Slave（``registry_remote_slave``）时，
    本次上报的全部变化（新增 / 更新 / 移除）直接生效；
  - **规则 A（驱动包升级）**：条目所属驱动包的版本号（PEP 440）比已接受内容的包版本高时直接
    生效；新条目与同一驱动包已接受条目里最高的包版本比较；

  其余由前端 ``apply`` 确认或 ``dismiss`` 忽略（批量接口同语义）。变化删改了被活跃 workflow
  引用的 action 时附冲突明细：挂起时供前端标出受影响节点，规则命中直接生效时记进上报统计；
- 上报集合里消失的既有条目：规则 B 成立时软移除（版本历史保留，可 ``restore`` 或再次上报复活），
  否则挂起为待确认移除；
- 每次上报落一条批次统计（新增/更新/挂起/移除/复活/不变/不可用/自动接受与规则 B 判定）；
- 为调度器提供 ``materials_need_lock`` 镜像查询（读生效版本）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid as uuid_module
from dataclasses import dataclass
from typing import Any, Callable, Collection, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence

try:  # 200 个条目的 payload 合计十几 MB，orjson 解析比标准库快数倍
    import orjson as _orjson
except ImportError:  # pragma: no cover - orjson 是可选加速
    _orjson = None

from packaging.version import InvalidVersion, Version

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.runtime.registry import (
    RegistryAcceptance,
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
    RegistryRemoteSlaveRecord,
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

#: 规则 B 不成立的原因（``RegistryPendingReason``）。
REASON_REMOTE_REPORTER = "cross-machine-reporter"
REASON_REMOTE_SLAVE = "cross-machine-slave"
#: 自动接受依据（``RegistryAcceptRule``）。
RULE_SINGLE_MACHINE = "single-machine"
RULE_PACKAGE_UPGRADE = "package-upgrade"

#: 返回活跃 workflow 节点对模板 action 的引用明细行，字段见
#: ``WorkflowService.list_template_action_references``（template_uuid / action /
#: node_uuid / node_name / workflow_uuid / workflow_name）。
ReferenceRowsResolver = Callable[[], Sequence[Mapping[str, Any]]]


class RegistryAuthorityError(RuntimeError):
    """上报内容不合法或条目操作无法执行。"""


def template_uuid(name: str) -> str:
    """按模板 id 派生稳定 uuid，统一上报身份与 workflow 引用。"""

    return str(uuid_module.uuid5(REGISTRY_TEMPLATE_NAMESPACE, str(name)))


@dataclass(frozen=True)
class RegistryReporter:
    """一次上报（或 Slave 告知）的来源。

    ``in_process``：与权威同进程的上报（``--no-safe-restart``），必然同机；否则以 ``machine_id``
    与权威自己的机器标识比较，空值按跨机器处理。``slaves`` 是 Host 当时连着的 Slave
    （``{"machine_name", "machine_id"}``），跨机器的会被记进 ``registry_remote_slave``。
    """

    edge_uuid: str = ""
    machine_id: str = ""
    slaves: tuple[Mapping[str, Any], ...] = ()
    in_process: bool = False

    @classmethod
    def local(cls, *, edge_uuid: str = "", slaves: Iterable[Mapping[str, Any]] = ()) -> "RegistryReporter":
        return cls(edge_uuid=edge_uuid, slaves=tuple(slaves), in_process=True)


class _Reported(NamedTuple):
    """规范化后的一个上报条目：完整定义（只发哈希时为 None）、声明的哈希、所属驱动包。"""

    payload: Optional[Dict[str, Any]]
    claimed_sha: Optional[str]
    package: str = ""
    package_version: str = ""


@dataclass(frozen=True)
class _Acceptance:
    single_machine: bool
    reason: str
    reporter_machine_id: str
    remote_slaves: tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return RegistryAcceptance(
            single_machine=self.single_machine,
            reason=self.reason,  # type: ignore[arg-type] - 只放合法原因
            reporter_machine_id=self.reporter_machine_id,
            remote_slaves=list(self.remote_slaves),
        ).model_dump(mode="python")


def _parse_version(value: str) -> Optional[Version]:
    try:
        return Version(str(value or "").strip()) if str(value or "").strip() else None
    except InvalidVersion:
        return None


class RegistryService(SqliteDomain):
    """串行化 registry 四表读写，维护生效条目的内存索引。

    生产环境表落 ``runtime.db``：把 RuntimeService 实例作为 ``database``
    传入即共享其 connection 与 write_lock（同库单连接单写者）；测试可传
    路径独立建一个 runtime 库（同一 RUNTIME_DATABASE 规格）。``machine_id`` 是权威
    自己的机器标识（缺省取 ``utils/machine_identity.machine_id()``），规则 B 拿上报的与它比较。
    """

    def __init__(
        self,
        database: DomainDatabase,
        *,
        reference_rows_resolver: Optional[ReferenceRowsResolver] = None,
        machine_id: Optional[str] = None,
    ) -> None:
        super().__init__(database, RUNTIME_DATABASE)
        self._resolver = reference_rows_resolver
        self._lock = threading.RLock()
        if machine_id is None:
            from unilabos.utils.machine_identity import machine_id as local_machine_id

            machine_id = local_machine_id()
        self.machine_id = str(machine_id)
        # 生效条目的 content_sha256 索引：上报比对只看哈希，不必为每个条目重算 payload 哈希
        self._active_hashes: Dict[str, str] = {}
        self._active: Dict[str, Dict[str, Any]] = self._load_active_payloads()
        self._reported = False

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
                    restored_from,content_sha256,package_name,package_version,payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    values["name"],
                    values["version"],
                    values["created_at_ms"],
                    values["source"],
                    values["edge_uuid"],
                    values["restored_from"],
                    values["content_sha256"],
                    values["package_name"],
                    values["package_version"],
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

    def _entry_sha(self, name: str, version: int) -> Optional[str]:
        with self.write_lock:
            row = self.connection.execute(
                "SELECT content_sha256 FROM registry_entry WHERE name=? AND version=?",
                (name, version),
            ).fetchone()
            return str(row[0]) if row is not None else None

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
                    pending_conflicts,unusable_reason,removed_at_ms,updated_at_ms,
                    package_name,package_version,pending_reason,pending_removal_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    template_uuid=excluded.template_uuid,
                    active_version=excluded.active_version,
                    pending_version=excluded.pending_version,
                    pending_conflicts=excluded.pending_conflicts,
                    unusable_reason=excluded.unusable_reason,
                    removed_at_ms=excluded.removed_at_ms,
                    updated_at_ms=excluded.updated_at_ms,
                    package_name=excluded.package_name,
                    package_version=excluded.package_version,
                    pending_reason=excluded.pending_reason,
                    pending_removal_at_ms=excluded.pending_removal_at_ms
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
                    values["package_name"],
                    values["package_version"],
                    values["pending_reason"],
                    values["pending_removal_at_ms"],
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

    def has_report(self) -> bool:
        """是否收到过 Host 的上报（全新库在首次上报前为 False，目录为空）。"""

        if self._reported:
            return True
        with self.write_lock:
            row = self.connection.execute("SELECT 1 FROM registry_report LIMIT 1").fetchone()
        self._reported = row is not None
        return self._reported

    # ── registry_remote_slave（跨机器 Slave） ─────────────────────

    def record_slaves(self, reporter: RegistryReporter) -> List[str]:
        """记下 Host 报来的 Slave 里不在权威这台机器上的；返回本次新记下的 machine_name。

        机器标识等于权威的 Slave 是本机的（受管 Slave 必然如此）：不记录，之前因换机而留下的
        同名记录随之删除。没有机器标识的旧 Slave 按跨机器记录。
        """

        now_ms = int(time.time() * 1000)
        added: List[str] = []
        with self._lock:
            for raw in reporter.slaves:
                name = str((raw or {}).get("machine_name") or "").strip()
                if not name:
                    continue
                slave_machine = str((raw or {}).get("machine_id") or "").strip()
                with self.write():
                    if slave_machine and slave_machine == self.machine_id:
                        self.connection.execute(
                            "DELETE FROM registry_remote_slave WHERE machine_name=?", (name,)
                        )
                        continue
                    existed = self.connection.execute(
                        "SELECT 1 FROM registry_remote_slave WHERE machine_name=?", (name,)
                    ).fetchone()
                    self.connection.execute(
                        """
                        INSERT INTO registry_remote_slave(
                            machine_name,machine_id,edge_uuid,first_seen_at_ms,last_seen_at_ms
                        ) VALUES (?,?,?,?,?)
                        ON CONFLICT(machine_name) DO UPDATE SET
                            machine_id=excluded.machine_id,
                            edge_uuid=excluded.edge_uuid,
                            last_seen_at_ms=excluded.last_seen_at_ms
                        """,
                        (name, slave_machine, str(reporter.edge_uuid or ""), now_ms, now_ms),
                    )
                if existed is None:
                    added.append(name)
        if added:
            logger.warning(
                "[Registry] 记录到跨机器 Slave %s：之后的注册表变化需在前端确认（驱动包版本升高的除外）",
                ", ".join(added),
            )
        return added

    def list_remote_slaves(self) -> List[Dict[str, Any]]:
        with self.write_lock:
            rows = self.connection.execute(
                "SELECT * FROM registry_remote_slave ORDER BY machine_name"
            ).fetchall()
        return [RegistryRemoteSlaveRecord.model_validate(dict(row)).model_dump(mode="python") for row in rows]

    def forget_remote_slave(self, machine_name: str) -> Dict[str, Any]:
        """删除一条跨机器 Slave 记录（用户确认它不再使用）；不存在时报错。"""

        with self._lock:
            with self.write():
                cursor = self.connection.execute(
                    "DELETE FROM registry_remote_slave WHERE machine_name=?", (str(machine_name or ""),)
                )
            if cursor.rowcount == 0:
                raise RegistryAuthorityError(f"remote slave {machine_name} not found")
        return {"machine_name": str(machine_name), "remaining": [row["machine_name"] for row in self.list_remote_slaves()]}

    def acceptance(self, reporter: Optional[RegistryReporter] = None) -> Dict[str, Any]:
        """规则 B 判定（不改任何状态）；``reporter`` 缺省视为与权威同机的 Host。"""

        return self._acceptance(reporter or RegistryReporter.local()).as_dict()

    def _acceptance(self, reporter: RegistryReporter) -> _Acceptance:
        reporter_machine = self.machine_id if reporter.in_process else str(reporter.machine_id or "")
        remote = tuple(row["machine_name"] for row in self.list_remote_slaves())
        if reporter_machine != self.machine_id:
            reason = REASON_REMOTE_REPORTER
        elif remote:
            reason = REASON_REMOTE_SLAVE
        else:
            reason = ""
        return _Acceptance(
            single_machine=not reason,
            reason=reason,
            reporter_machine_id=reporter_machine,
            remote_slaves=remote,
        )

    def status(self) -> Dict[str, Any]:
        """目录状态：是否收到过上报、最近一次上报、挂起数、当前的规则 B 判定。"""

        with self.write_lock:
            row = self.connection.execute(
                "SELECT * FROM registry_report ORDER BY report_id DESC LIMIT 1"
            ).fetchone()
        last_report = None
        if row is not None:
            values = dict(row)
            summary = _load(values["summary"])
            last_report = {
                "report_id": values["report_id"],
                "created_at_ms": values["created_at_ms"],
                "edge_uuid": values["edge_uuid"],
                "counts": summary.get("counts", {}),
                "acceptance": summary.get("acceptance"),
            }
        with self._lock:
            states = self._list_states()
        return {
            "reported": last_report is not None,
            "last_report": last_report,
            "active_count": len(self._active),
            "pending_count": sum(1 for state in states if _has_pending(state)),
            "machine_id": self.machine_id,
            "acceptance": self.acceptance(),
            "remote_slaves": self.list_remote_slaves(),
        }

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
                        sha = self._entry_sha(state.name, state.active_version)
                    if sha is not None:
                        active[state.name] = sha
                if state.pending_version is not None:
                    sha = self._entry_sha(state.name, state.pending_version)
                    if sha is not None:
                        pending[state.name] = sha
        return {"active": active, "pending": pending}

    def report(
        self,
        definitions: Sequence[Mapping[str, Any]],
        *,
        edge_uuid: str = "",
        reporter: Optional[RegistryReporter] = None,
    ) -> Dict[str, Any]:
        """全量上报（旧形状：每个条目都是完整定义，不带驱动包信息）。

        ``reporter`` 缺省为进程内上报（与权威同机）。
        """

        entries, unusable = _normalize_definitions(definitions)
        return self._report(
            {name: _Reported(entry, None) for name, entry in entries.items()},
            unusable,
            reporter=_with_edge(reporter, edge_uuid),
        )

    def report_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        edge_uuid: str = "",
        reporter: Optional[RegistryReporter] = None,
    ) -> Dict[str, Any]:
        """按哈希增量的全量上报：``[{id, content_sha256, payload?, package?, package_version?}]``。

        带 payload 的条目与旧形状同样处理（哈希由权威自己重算）；只带哈希的条目必须命中
        权威已有的生效 / 挂起版本，否则进结果的 ``missing``，由 Host 补 payload 再报。
        """

        normalized: Dict[str, _Reported] = {}
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
            normalized[name] = _Reported(
                dict(payload) if payload is not None else None,
                claimed or None,
                str(raw.get("package") or "").strip(),
                str(raw.get("package_version") or "").strip(),
            )
        return self._report(normalized, unusable, reporter=_with_edge(reporter, edge_uuid))

    def _report(
        self,
        entries: Mapping[str, _Reported],
        unusable: List[Dict[str, str]],
        *,
        reporter: RegistryReporter,
    ) -> Dict[str, Any]:
        referenced = self._referenced_actions()
        now_ms = int(time.time() * 1000)
        edge_uuid = str(reporter.edge_uuid or "")

        detail: Dict[str, Any] = {
            "added": [],
            "updated": [],
            "pending": [],
            "unchanged": [],
            "removed": [],
            "revived": [],
            "unusable": unusable,
            "auto_accepted": [],
        }
        missing: List[str] = []

        with self._lock:
            # 先记下 Host 当时连着的跨机器 Slave，再做规则 B 判定
            self.record_slaves(reporter)
            acceptance = self._acceptance(reporter)
            accepted_versions = self._accepted_package_versions()
            for name, reported in entries.items():
                if reported.payload is not None:
                    self._report_entry(
                        name, reported, referenced, now_ms, edge_uuid, acceptance, accepted_versions, detail
                    )
                elif not self._report_hash_only(
                    name, reported, referenced, now_ms, acceptance, accepted_versions, detail
                ):
                    missing.append(name)

            # 上报集合外的既有条目：规则 B 成立时软移除，否则挂起为待确认移除；只挂起着、
            # 从未生效的新条目直接撤回挂起。只发哈希、尚待补 payload 的条目也在集合里，
            # 不会被误判为移除。
            reported_names = set(entries)
            for state in self._list_states():
                if state.name in reported_names:
                    continue
                self._report_absent(state, referenced, now_ms, acceptance, detail)

            summary = _summarize(detail, total=len(entries), acceptance=acceptance)
            report_id = self._insert_report(
                RegistryReportRecord(
                    created_at_ms=now_ms,
                    edge_uuid=edge_uuid,
                    summary=summary,
                )
            )
            self._reported = True

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

    def _accepted_package_versions(self) -> Dict[str, Version]:
        """各驱动包已接受（生效且未移除）条目里最高的包版本：新条目的规则 A 基准。"""

        best: Dict[str, Version] = {}
        for state in self._list_states():
            if state.active_version is None or state.removed_at_ms is not None or not state.package_name:
                continue
            parsed = _parse_version(state.package_version)
            if parsed is not None and (state.package_name not in best or parsed > best[state.package_name]):
                best[state.package_name] = parsed
        return best

    def _accept_rule(
        self,
        state: Optional[RegistryEntryStateRecord],
        reported: _Reported,
        acceptance: _Acceptance,
        accepted_versions: Mapping[str, Version],
    ) -> Optional[str]:
        """新增 / 变化条目的自动接受依据；None 表示挂起待确认。"""

        if acceptance.single_machine:
            return RULE_SINGLE_MACHINE
        new_version = _parse_version(reported.package_version)
        if not reported.package or new_version is None:
            return None
        if state is not None and state.active_version is not None:
            # 已有接受内容：只与它的包比较（换了包、旧记录没有包版本都不算升级）
            if state.package_name != reported.package:
                return None
            base = _parse_version(state.package_version)
        else:
            base = accepted_versions.get(reported.package)
        if base is not None and new_version > base:
            return RULE_PACKAGE_UPGRADE
        return None

    def _report_hash_only(
        self,
        name: str,
        reported: _Reported,
        referenced: Mapping[str, Collection[str]],
        now_ms: int,
        acceptance: _Acceptance,
        accepted_versions: Mapping[str, Version],
        detail: Dict[str, Any],
    ) -> bool:
        """只带哈希的条目：命中生效版本按"未变"（软移除的则复活），命中挂起版本按"仍挂起"
        （此时接受规则成立则直接生效）。

        Returns:
            False 表示权威没有这个哈希的版本，需要 Host 补完整 payload。
        """

        claimed_sha = reported.claimed_sha or ""
        state = self._find_state(name)
        if state is None or not claimed_sha:
            return False
        active_sha = self._active_hashes.get(name)
        if state.active_version is not None and active_sha is None:
            # 软移除条目不在活跃索引中：从版本表读生效版本
            active_sha = self._entry_sha(name, state.active_version)

        if state.active_version is not None and active_sha == claimed_sha:
            self._keep_active(state, reported, now_ms, detail)
            return True

        if state.pending_version is not None:
            if self._entry_sha(name, state.pending_version) == claimed_sha:
                self._hold_pending(state, reported, referenced, now_ms, acceptance, accepted_versions, detail)
                return True
        return False

    def _keep_active(
        self,
        state: RegistryEntryStateRecord,
        reported: _Reported,
        now_ms: int,
        detail: Dict[str, Any],
    ) -> None:
        """上报内容与生效版本相同：不生成版本；软移除的复活，挂起的移除 / 过时的新版本撤回。"""

        name = state.name
        was_removed = state.removed_at_ms is not None
        changed = bool(state.unusable_reason) or was_removed or _has_pending(state)
        state.unusable_reason = ""
        # Host 又报回生效内容：之前挂起的新版本 / 移除已不是 Host 的现状
        state.pending_version = None
        state.pending_conflicts = []
        state.pending_reason = ""
        state.pending_removal_at_ms = None
        if reported.package and (
            state.package_name != reported.package or state.package_version != reported.package_version
        ):
            # 规则 A 的基准跟着 Host 现在交付这份内容的包版本走
            state.package_name = reported.package
            state.package_version = reported.package_version
            changed = True
        if was_removed:
            state.removed_at_ms = None
            detail["revived"].append(name)
            if state.active_version is not None:
                stored = self.find_entry(name, state.active_version)
                if stored is not None:
                    self._set_active(name, stored.payload, stored.content_sha256)
        else:
            detail["unchanged"].append(name)
        if changed:
            state.updated_at_ms = now_ms
            self._upsert_state(state)

    def _hold_pending(
        self,
        state: RegistryEntryStateRecord,
        reported: _Reported,
        referenced: Mapping[str, Collection[str]],
        now_ms: int,
        acceptance: _Acceptance,
        accepted_versions: Mapping[str, Version],
        detail: Dict[str, Any],
    ) -> None:
        """同一份挂起内容再报一次：不再生成新版本；接受规则这次成立就直接生效。"""

        rule = self._accept_rule(state, reported, acceptance, accepted_versions)
        if rule is None:
            state.pending_reason = acceptance.reason
            state.pending_removal_at_ms = None
            state.updated_at_ms = now_ms
            self._upsert_state(state)
            detail["pending"].append(_pending_item(state, acceptance.reason))
            return
        pending = self.find_entry(state.name, int(state.pending_version or 0))
        if pending is None:
            raise RegistryAuthorityError(f"entry {state.name} pending version missing")
        conflicts = self._conflicts_against_active(state, pending.payload, referenced)
        was_new = state.active_version is None
        self._activate(state, pending, reported, now_ms)
        detail["added" if was_new else "updated"].append(state.name)
        detail["auto_accepted"].append({"name": state.name, "rule": rule, "conflicts": conflicts})

    def _report_entry(
        self,
        name: str,
        reported: _Reported,
        referenced: Mapping[str, Collection[str]],
        now_ms: int,
        edge_uuid: str,
        acceptance: _Acceptance,
        accepted_versions: Mapping[str, Version],
        detail: Dict[str, Any],
    ) -> None:
        entry = dict(reported.payload or {})
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
        active_sha = self._active_hashes.get(name)
        if state is not None and state.active_version is not None and active_sha is None:
            # 软移除条目不在活跃索引中，需从版本表读取生效内容的哈希。
            active_sha = self._entry_sha(name, state.active_version)

        if state is not None and state.active_version is not None and active_sha == content_sha:
            # 相同内容不会生成版本；重新上报可恢复软移除条目。
            self._keep_active(state, reported, now_ms, detail)
            return

        if (
            state is not None
            and state.pending_version is not None
            and self._entry_sha(name, state.pending_version) == content_sha
        ):
            self._hold_pending(state, reported, referenced, now_ms, acceptance, accepted_versions, detail)
            return

        # 同一份内容刚被忽略过（最新版本行既不生效也不挂起）：复用那个版本号，不重复存一份
        version = self._max_entry_version(name)
        latest = self.find_entry(name, version) if version else None
        if not (
            latest is not None
            and latest.content_sha256 == content_sha
            and (state is None or latest.version != state.active_version)
        ):
            version += 1
            latest = RegistryEntryRecord(
                name=name,
                version=version,
                created_at_ms=now_ms,
                source="edge-report",
                edge_uuid=edge_uuid,
                content_sha256=content_sha,
                package_name=reported.package,
                package_version=reported.package_version,
                payload=entry,
            )
            self._insert_entry(latest)

        record = state or _new_state(name, now_ms)
        record.unusable_reason = ""
        conflicts = self._conflicts_against_active(record, entry, referenced)
        rule = self._accept_rule(state, reported, acceptance, accepted_versions)
        if rule is not None:
            was_new = record.active_version is None
            if record.removed_at_ms is not None:
                detail["revived"].append(name)
            self._activate(record, latest, reported, now_ms)
            detail["added" if was_new else "updated"].append(name)
            detail["auto_accepted"].append({"name": name, "rule": rule, "conflicts": conflicts})
            return

        # 挂起待确认：生效版本（以及软移除状态）不动
        record.pending_version = version
        record.pending_conflicts = conflicts
        record.pending_reason = acceptance.reason
        record.pending_removal_at_ms = None
        record.updated_at_ms = now_ms
        self._upsert_state(record)
        detail["pending"].append(_pending_item(record, acceptance.reason))

    def _report_absent(
        self,
        state: RegistryEntryStateRecord,
        referenced: Mapping[str, Collection[str]],
        now_ms: int,
        acceptance: _Acceptance,
        detail: Dict[str, Any],
    ) -> None:
        """Host 这次没有上报的既有条目。"""

        if state.active_version is None or state.removed_at_ms is not None:
            # 从未生效的新条目 / 已软移除的条目：Host 不再提供，撤回挂起的版本
            if state.pending_version is not None:
                state.pending_version = None
                state.pending_conflicts = []
                state.pending_reason = ""
                state.updated_at_ms = now_ms
                self._upsert_state(state)
            return
        conflicts = [
            {"action": action, "reason": "action-removed"}
            for action in sorted(
                {str(item) for item in referenced.get(state.template_uuid, ())}
                & set(_action_definitions(self._active.get(state.name) or {}))
            )
        ]
        if acceptance.single_machine:
            state.removed_at_ms = now_ms
            state.pending_version = None
            state.pending_conflicts = []
            state.pending_reason = ""
            state.pending_removal_at_ms = None
            state.updated_at_ms = now_ms
            self._upsert_state(state)
            self._drop_active(state.name)
            detail["removed"].append(state.name)
            detail["auto_accepted"].append(
                {"name": state.name, "rule": RULE_SINGLE_MACHINE, "conflicts": conflicts}
            )
            return
        # 待确认移除替换掉之前挂起的新版本：Host 现在连这个条目都没有了
        if state.pending_removal_at_ms is None:
            state.pending_removal_at_ms = now_ms
        state.pending_version = None
        state.pending_conflicts = conflicts
        state.pending_reason = acceptance.reason
        state.updated_at_ms = now_ms
        self._upsert_state(state)
        detail["pending"].append(_pending_item(state, acceptance.reason))

    def _conflicts_against_active(
        self,
        state: RegistryEntryStateRecord,
        candidate: Mapping[str, Any],
        referenced: Mapping[str, Collection[str]],
    ) -> List[Dict[str, str]]:
        if state.active_version is None:
            return []
        active_payload = self._active.get(state.name)
        if active_payload is None:
            stored = self.find_entry(state.name, state.active_version)
            active_payload = stored.payload if stored is not None else {}
        return _detect_conflicts(active_payload, candidate, referenced.get(state.template_uuid, ()))

    def _activate(
        self,
        state: RegistryEntryStateRecord,
        entry: RegistryEntryRecord,
        reported: Optional[_Reported],
        now_ms: int,
    ) -> None:
        """把版本行设为生效（复活软移除），清掉挂起；包基准取上报的（没有则取版本行的）。"""

        state.active_version = entry.version
        state.pending_version = None
        state.pending_conflicts = []
        state.pending_reason = ""
        state.pending_removal_at_ms = None
        state.unusable_reason = ""
        state.removed_at_ms = None
        if reported is not None and reported.package:
            state.package_name = reported.package
            state.package_version = reported.package_version
        else:
            state.package_name = entry.package_name
            state.package_version = entry.package_version
        state.updated_at_ms = now_ms
        self._upsert_state(state)
        self._set_active(state.name, entry.payload, entry.content_sha256)

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
        """接受挂起的变化：挂起版本切换为生效版本，待确认移除则软移除。"""

        with self._lock:
            state = self._require_state(name)
            now_ms = int(time.time() * 1000)
            if state.pending_removal_at_ms is not None:
                state.removed_at_ms = now_ms
                state.pending_removal_at_ms = None
                state.pending_conflicts = []
                state.pending_reason = ""
                state.updated_at_ms = now_ms
                self._upsert_state(state)
                self._drop_active(state.name)
                return self._entry_summary(state)
            if state.pending_version is None:
                raise RegistryAuthorityError(f"entry {name} has no pending version")
            entry = self.find_entry(name, state.pending_version)
            if entry is None:
                raise RegistryAuthorityError(
                    f"entry {name} pending version {state.pending_version} missing"
                )
            self._activate(state, entry, None, now_ms)
            return self._entry_summary(state)

    def dismiss_pending(self, name: str) -> Dict[str, Any]:
        """忽略挂起的变化（版本行保留在历史里，生效版本不动）。

        Host 下次上报仍是这份内容时会再次挂起（复用同一版本号）：挂起表示 Host 的现状与
        已接受目录不一致，只要不一致就会提示。
        """

        with self._lock:
            state = self._require_state(name)
            if state.pending_version is None and state.pending_removal_at_ms is None:
                raise RegistryAuthorityError(f"entry {name} has no pending version")
            state.pending_version = None
            state.pending_removal_at_ms = None
            state.pending_conflicts = []
            state.pending_reason = ""
            state.updated_at_ms = int(time.time() * 1000)
            self._upsert_state(state)
            return self._entry_summary(state)

    def apply_pending_many(self, names: Sequence[str] = ()) -> Dict[str, Any]:
        """批量接受；``names`` 为空时接受全部挂起条目。逐条执行，失败的列入 ``failed``。"""

        return self._pending_many(self.apply_pending, names)

    def dismiss_pending_many(self, names: Sequence[str] = ()) -> Dict[str, Any]:
        """批量忽略；``names`` 为空时忽略全部挂起条目。"""

        return self._pending_many(self.dismiss_pending, names)

    def _pending_many(self, action: Callable[[str], Dict[str, Any]], names: Sequence[str]) -> Dict[str, Any]:
        with self._lock:
            targets = [str(name) for name in names if str(name).strip()] or [
                state.name for state in self._list_states() if _has_pending(state)
            ]
            done: List[Dict[str, Any]] = []
            failed: List[Dict[str, str]] = []
            for name in dict.fromkeys(targets):
                try:
                    done.append(action(name))
                except RegistryAuthorityError as exc:
                    failed.append({"name": name, "error": str(exc)})
        return {"entries": done, "failed": failed}

    def restore(self, name: str, version: int) -> Dict[str, Any]:
        """把历史版本内容还原为新的生效版本（版本号继续自增）。

        显式人工操作：不走接受规则，同时清掉挂起的变化、复活软移除条目。
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
                and not _has_pending(state)
                and name in self._active
                and self._active_hashes.get(name) == snapshot.content_sha256
            ):
                return self._entry_summary(state)

            new_version = self._max_entry_version(name) + 1
            restored = RegistryEntryRecord(
                name=name,
                version=new_version,
                created_at_ms=now_ms,
                source="restore",
                edge_uuid=snapshot.edge_uuid,
                restored_from=snapshot.version,
                content_sha256=snapshot.content_sha256,
                package_name=snapshot.package_name,
                package_version=snapshot.package_version,
                payload=snapshot.payload,
            )
            self._insert_entry(restored)
            self._activate(state, restored, None, now_ms)
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
            pending_states = [state for state in self._list_states() if _has_pending(state)]
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
                    pending_change=_pending_change(state),  # type: ignore[arg-type]
                    pending_reason=state.pending_reason,  # type: ignore[arg-type]
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
                "package_name": record.package_name,
                "package_version": record.package_version,
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

    def active_payloads(self, registry_type: str = "") -> List[Dict[str, Any]]:
        """生效条目 payload（按名称排序）；``registry_type`` 非空时只取该类型（如 ``device``）。"""

        with self._lock:
            payloads = [
                dict(payload)
                for _name, payload in sorted(self._active.items())
                if not registry_type or str(payload.get("registry_type") or "") == registry_type
            ]
        return payloads

    def active_payload(self, name: str, registry_type: str = "") -> Optional[Dict[str, Any]]:
        """单个生效条目的 payload（浅拷贝）；不存在、已移除或类型不符时为 None。"""

        with self._lock:
            payload = self._active.get(str(name or ""))
        if payload is None or (registry_type and str(payload.get("registry_type") or "") != registry_type):
            return None
        return dict(payload)

    def active_fingerprint(self) -> str:
        """生效条目集合的内容指纹（名称 + 内容哈希）；模板投影据此判断是否需要重算。"""

        with self._lock:
            pairs = sorted(self._active_hashes.items())
        return canonical_hash(pairs)

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

    def _entry_summary(self, state: RegistryEntryStateRecord) -> Dict[str, Any]:
        status: List[str] = []
        if state.removed_at_ms is not None:
            status.append("removed")
        elif state.active_version is not None:
            status.append("active")
        if _has_pending(state):
            status.append("pending")
        if state.unusable_reason:
            status.append("unusable")
        pending_package_version = ""
        if state.pending_version is not None:
            pending = self._list_entry_package(state.name, state.pending_version)
            pending_package_version = pending[1] if pending is not None else ""
        return RegistryEntrySummary(
            name=state.name,
            template_uuid=state.template_uuid,
            active_version=state.active_version,
            pending_version=state.pending_version,
            pending_conflicts=list(state.pending_conflicts),
            pending_change=_pending_change(state),  # type: ignore[arg-type]
            pending_reason=state.pending_reason,  # type: ignore[arg-type]
            pending_package_version=pending_package_version,
            package=state.package_name,
            package_version=state.package_version,
            unusable_reason=state.unusable_reason,
            removed_at_ms=state.removed_at_ms,
            updated_at_ms=state.updated_at_ms,
            status=status,  # type: ignore[arg-type] - 上面只放合法标签
        ).model_dump(mode="python")

    def _list_entry_package(self, name: str, version: int) -> Optional[tuple[str, str]]:
        with self.write_lock:
            row = self.connection.execute(
                "SELECT package_name, package_version FROM registry_entry WHERE name=? AND version=?",
                (name, version),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row is not None else None


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


def _has_pending(state: RegistryEntryStateRecord) -> bool:
    return state.pending_version is not None or state.pending_removal_at_ms is not None


def _pending_change(state: RegistryEntryStateRecord) -> str:
    if state.pending_removal_at_ms is not None:
        return "remove"
    if state.pending_version is not None:
        return "add" if state.active_version is None else "update"
    return ""


def _pending_item(state: RegistryEntryStateRecord, reason: str) -> Dict[str, Any]:
    return {
        "name": state.name,
        "conflicts": list(state.pending_conflicts),
        "change": _pending_change(state),
        "reason": reason,
    }


def _with_edge(reporter: Optional[RegistryReporter], edge_uuid: str) -> RegistryReporter:
    """缺省上报者为进程内（与权威同机）；显式给的 ``edge_uuid`` 覆盖上报者里的。"""

    reporter = reporter or RegistryReporter.local()
    if edge_uuid and edge_uuid != reporter.edge_uuid:
        return RegistryReporter(
            edge_uuid=edge_uuid,
            machine_id=reporter.machine_id,
            slaves=reporter.slaves,
            in_process=reporter.in_process,
        )
    return reporter

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


def _summarize(detail: Mapping[str, Any], *, total: int, acceptance: Optional[_Acceptance] = None) -> Dict[str, Any]:
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
            "auto_accepted": len(detail.get("auto_accepted", [])),
        },
        added=list(detail.get("added", [])),
        updated=list(detail.get("updated", [])),
        pending=list(detail.get("pending", [])),
        removed=list(detail.get("removed", [])),
        revived=list(detail.get("revived", [])),
        unusable=list(detail.get("unusable", [])),
        auto_accepted=list(detail.get("auto_accepted", [])),
        acceptance=acceptance.as_dict() if acceptance is not None else None,
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
    "REASON_REMOTE_REPORTER",
    "REASON_REMOTE_SLAVE",
    "REGISTRY_TEMPLATE_NAMESPACE",
    "RULE_PACKAGE_UPGRADE",
    "RULE_SINGLE_MACHINE",
    "ReferenceRowsResolver",
    "RegistryAuthorityError",
    "RegistryReporter",
    "RegistryService",
    "get_registry_service",
    "set_registry_service",
    "template_uuid",
]
