"""V1 进程管理协议（原 Workspace Host 路由）在 dev 进程管理上的实现（快照、幂等操作、组件日志）。

dev 没有 V1 的独立工作区监督进程：``unilab`` 调度权威进程本身就是监督者，常驻并持有管理端口，
直接看护 Host 子进程（``app/supervisor.py:HostChildSupervisor``）；受管 Slave / 设备进程由 Host
的 ``services/device_processes.py`` 管理。本模块把 V1 的四个路由映射到这些既有事实上：

组件
    ``backend`` = 调度权威（本进程，常驻）；``edge`` = Host（默认拓扑是本进程看护的子进程，
    ``--role backend`` 是经控制面接入的远端 Host，``--no-safe-restart`` 单进程时就是本进程）；
    ``managed:<process_id>`` = Host 的每个受管设备进程；``plc`` / ``renderer`` dev 不运行，恒为 idle。
操作
    一个 FIFO 工作线程串行执行（V1 同口径）：Host 启停走 ``HostChildSupervisor``，Host 重启走
    ``RestartCoordinator``（安静点、scope=edge），排空 / 恢复走 ``SchedulerLifecycle``，受管进程与
    驱动包经控制面交给 Host 的 ``/api/v1/device-processes``、``/api/v1/driver-packages``（驱动包操作
    沿用 Host 的操作台账，本记录只挂 ``packageOperationId``），物料布局走物料快照比较 / 应用。
持久化
    操作记录与快照修订号在 runtime.db（v12 ``process_management_operation`` / ``process_management_state``）。
    修订号按观测到的进程状态指纹变化或成功的写操作递增，``expectedRevision`` 与它比较。
    权威进程重启后，未完成的操作标记为 ``operation_interrupted`` 失败，不自动重放。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import queue
import re
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from unilabos.protocol.runtime.process_management import (
    MANAGED_COMPONENT_PREFIX,
    MANAGED_PROCESS_ID,
    MAX_LOG_MAX_BYTES,
    OPERATION_ID_MAX_LENGTH,
    SCHEMA_VERSION,
    SUPPORTED_COMMANDS,
    UNSUPPORTED_COMPONENTS,
    UNSUPPORTED_V1_COMMANDS,
    ProcessManagementError,
)

logger = logging.getLogger(__name__)

#: 只读命令：成功也不递增修订号。
READ_ONLY_COMMANDS = frozenset({"material.layout.inspect", "material.layout.preview", "backend.start"})
BACKEND_CAPABILITIES = ["authoring", "inventory", "workflow-run", "station-scheduler"]
#: 等待预算缺省值（秒）：Host 就绪 / 排空与 V1 的 readiness 总预算一致；重启覆盖协调器自身的
#: 断开（1200s）+ 重连（300s）窗口；驱动包安装可能持续十几分钟。
DEFAULT_READY_TIMEOUT_S = 600.0
DEFAULT_DRAIN_TIMEOUT_S = 600.0
DEFAULT_RESTART_TIMEOUT_S = 1800.0
DEFAULT_PACKAGE_TIMEOUT_S = 3600.0
MAX_TIMEOUT_S = 7200.0
#: 快照读取 Host 受管进程列表的单次预算：Host 卡住时快照不能跟着挂一分钟。
SNAPSHOT_HOST_TIMEOUT_S = 5.0
HOST_ACTION_TIMEOUT_S = 60.0
LOG_READ_LINES = 1000
_PREVIEW_ID = re.compile(r"^[0-9a-f]{64}$")


def _utc_timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _plain(value: Any) -> Any:
    """协调器 / 调度器状态里可能夹带非 JSON 对象，统一转成可落库的 JSON 值。"""

    return json.loads(_dumps(value))


# ── 运行形态（由 backend_main 装配时登记） ───────────────────────────


@dataclass
class ProcessManagementContext:
    """调度权威进程的运行形态：管理端口、Host 子进程看护者与库目录。"""

    management_port: Optional[int] = None
    supervisor: Any = None
    database_root: str = ""
    edge_database_root: str = ""


_context = ProcessManagementContext()


def configure_process_management(
    *,
    management_port: Optional[int] = None,
    supervisor: Any = None,
    database_root: str = "",
    edge_database_root: str = "",
) -> None:
    """``app/backend_main.py`` 装配后登记；停机时传默认值清空。"""

    global _context
    _context = ProcessManagementContext(
        management_port=management_port,
        supervisor=supervisor,
        database_root=database_root,
        edge_database_root=edge_database_root,
    )


def process_management_context() -> ProcessManagementContext:
    return _context


def _working_dir() -> Path:
    from unilabos.config.config import BasicConfig

    return Path(getattr(BasicConfig, "working_dir", None) or Path.cwd()).expanduser().resolve()


# ── Host 访问口 ─────────────────────────────────────────────────────


class HostGateway:
    """Host 专有能力（受管进程、驱动包、日志）的访问口。

    分进程（调度权威 + Host）经控制面 ``backend_http`` 让 Host 在进程内执行既有路由；
    单进程（``--no-safe-restart``）直接调用本进程的服务。错误统一成 ``ProcessManagementError``。
    """

    def remote(self) -> bool:
        from unilabos.server.api.edge_proxy import edge_proxy_enabled

        return edge_proxy_enabled()

    # 分进程：经控制面让 Host 执行一条既有路由，返回信封 data
    def _call(self, method: str, path: str, body: Any = None, *, timeout: float = HOST_ACTION_TIMEOUT_S) -> Any:
        from unilabos.client.utils.envelope import EnvelopeError
        from unilabos.server.api.edge_proxy import edge_http, edge_response_data

        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"content-type": "application/json"} if body is not None else {}
        response = edge_http(method, path, headers=headers, body=payload, timeout=timeout)
        if response is None:
            raise ProcessManagementError("host_unreachable", "Host 未接入控制面或未在预算内响应")
        try:
            return edge_response_data(response)
        except EnvelopeError as exc:
            raise _host_error(exc.status_code, exc.error, {"businessCode": exc.code, "path": path}) from exc
        except ValueError as exc:
            raise ProcessManagementError("host_protocol_invalid", f"Host 响应无效：{exc}") from exc

    def list_processes(self, *, timeout: float = SNAPSHOT_HOST_TIMEOUT_S) -> list[dict[str, Any]]:
        if self.remote():
            data = self._call("GET", "/api/v1/device-processes", timeout=timeout)
            return list(data.get("processes") or []) if isinstance(data, dict) else []
        from unilabos.server.services.device_processes import get_device_process_service

        return get_device_process_service().list()

    def process_action(self, process_id: str, action: str) -> dict[str, Any]:
        if self.remote():
            return self._call("POST", f"/api/v1/device-processes/{quote(process_id, safe='')}/{action}", {})
        from unilabos.server.services.device_processes import (
            DeviceProcessBusyError,
            DeviceProcessError,
            get_device_process_service,
        )

        try:
            return getattr(get_device_process_service(), action)(process_id)
        except DeviceProcessError as exc:
            message = str(exc)
            status = 404 if "not found" in message else 409 if isinstance(exc, DeviceProcessBusyError) else 422
            raise _host_error(status, message, None) from exc

    def _package_call(self, name: str, *args: Any) -> dict[str, Any]:
        from unilabos.server.services.driver_packages import (
            DriverPackageBusyError,
            DriverPackageError,
            get_driver_package_service,
        )

        try:
            return getattr(get_driver_package_service(), name)(*args)
        except DriverPackageError as exc:
            message = str(exc)
            status = 409 if isinstance(exc, DriverPackageBusyError) else 404 if "not found" in message else 422
            raise _host_error(status, message, None) from exc

    def install_package(self, spec: str, enable: bool, upgrade: bool, name: str) -> dict[str, Any]:
        if self.remote():
            return self._call(
                "POST", "/api/v1/driver-packages/install",
                {"spec": spec, "enable": enable, "upgrade": upgrade, "name": name},
            )
        return self._package_call("start_install", spec, enable, upgrade, name)

    def uninstall_package(self, name: str) -> dict[str, Any]:
        if self.remote():
            return self._call("DELETE", f"/api/v1/driver-packages/{quote(name, safe='')}")
        return self._package_call("start_uninstall", name)

    def set_package_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        if self.remote():
            return self._call("PUT", f"/api/v1/driver-packages/{quote(name, safe='')}/enabled", {"enabled": enabled})
        return self._package_call("set_enabled", name, enabled)

    def package_operation(self, operation_id: str) -> dict[str, Any]:
        if self.remote():
            return self._call("GET", f"/api/v1/driver-packages/operations/{quote(operation_id, safe='')}")
        return self._package_call("operation", operation_id)

    def read_log(self, source_id: str, *, limit: int = LOG_READ_LINES) -> dict[str, Any]:
        """有界读取 Host 日志来源（``host`` / ``managed:<id>``）；不接受任何路径。"""

        if self.remote():
            return self._call(
                "GET",
                f"/api/v1/hostlink/logs?source_id={quote(source_id, safe='')}&limit={int(limit)}",
                timeout=SNAPSHOT_HOST_TIMEOUT_S * 2,
            )
        from unilabos.server.services.runtime.logs import (
            LogSourceNotFound,
            LogSourceUnavailable,
            get_runtime_log_service,
        )

        try:
            return get_runtime_log_service().read(source_id, limit=limit).model_dump()
        except LogSourceNotFound as exc:
            raise _host_error(404, str(exc), None) from exc
        except LogSourceUnavailable as exc:
            raise _host_error(503, str(exc), None) from exc


def _host_error(status: int, message: str, details: Any) -> ProcessManagementError:
    code = {404: "host_resource_not_found", 409: "host_busy", 503: "host_unavailable"}.get(
        int(status), "host_request_rejected"
    )
    return ProcessManagementError(code, message, details={"httpStatus": int(status), **(details or {})})


# ── runtime.db 存取 ─────────────────────────────────────────────────


class ProcessManagementStore:
    """``process_management_operation`` / ``process_management_state`` 的读写；共享 runtime.db 的写锁。"""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow
        with workflow.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO process_management_state(singleton, revision, fingerprint, updated_at) "
                "VALUES (1, 0, '', ?)",
                (_utc_timestamp(),),
            )

    def _read(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.workflow.write_lock:
            return list(self.workflow.connection.execute(sql, args).fetchall())

    def revision(self) -> int:
        return int(self._read("SELECT revision FROM process_management_state WHERE singleton=1")[0]["revision"])

    def observe(self, fingerprint: str) -> int:
        """进程状态指纹变了就递增修订号（比较并交换，并发观测只递增一次）。"""

        with self.workflow.transaction() as conn:
            row = conn.execute("SELECT revision, fingerprint FROM process_management_state WHERE singleton=1").fetchone()
            if row["fingerprint"] == fingerprint:
                return int(row["revision"])
            conn.execute(
                "UPDATE process_management_state SET revision=revision+1, fingerprint=?, updated_at=? WHERE singleton=1",
                (fingerprint, _utc_timestamp()),
            )
            return int(row["revision"]) + 1

    def bump(self) -> int:
        with self.workflow.transaction() as conn:
            conn.execute(
                "UPDATE process_management_state SET revision=revision+1, updated_at=? WHERE singleton=1",
                (_utc_timestamp(),),
            )
            return int(conn.execute("SELECT revision FROM process_management_state WHERE singleton=1").fetchone()[0])

    @staticmethod
    def _document(row: sqlite3.Row) -> dict[str, Any]:
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "operationId": row["operation_id"],
            "command": row["command"],
            "parameters": json.loads(row["parameters"]),
            "requestHash": row["request_hash"],
            "phase": row["phase"],
            "submittedAt": row["submitted_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "result": json.loads(row["result"]) if row["result"] is not None else None,
            "error": json.loads(row["error"]) if row["error"] is not None else None,
        }
        progress = json.loads(row["progress"] or "{}")
        if progress:
            # dev 扩展：执行中挂接的既有事实（驱动包操作 id、重启代次等）
            document["progress"] = progress
        return document

    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        rows = self._read("SELECT * FROM process_management_operation WHERE operation_id=?", (operation_id,))
        return self._document(rows[0]) if rows else None

    def submit(
        self,
        *,
        operation_id: str,
        command: str,
        parameters: dict[str, Any],
        expected_revision: Optional[int],
        request_hash: str,
        fingerprint: Optional[str],
    ) -> tuple[dict[str, Any], bool]:
        """幂等登记：已存在同指纹返回原记录；修订号不符拒绝；返回 (记录, 是否新建)。"""

        with self.workflow.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM process_management_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise ProcessManagementError("operation_conflict", f"operationId 已被不同请求使用：{operation_id}")
                return self._document(row), False
            state = conn.execute("SELECT revision, fingerprint FROM process_management_state WHERE singleton=1").fetchone()
            revision = int(state["revision"])
            if fingerprint is not None and fingerprint != state["fingerprint"]:
                revision += 1
                conn.execute(
                    "UPDATE process_management_state SET revision=?, fingerprint=?, updated_at=? WHERE singleton=1",
                    (revision, fingerprint, _utc_timestamp()),
                )
            if expected_revision is not None and expected_revision != revision:
                raise ProcessManagementError(
                    "revision_conflict",
                    "进程管理 revision 已变化",
                    details={"expected": expected_revision, "actual": revision},
                )
            conn.execute(
                "INSERT INTO process_management_operation(operation_id, command, parameters, expected_revision, "
                "request_hash, phase, submitted_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (operation_id, command, _dumps(parameters), expected_revision, request_hash, _utc_timestamp()),
            )
            created = conn.execute(
                "SELECT * FROM process_management_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return self._document(created), True

    def start(self, operation_id: str) -> Optional[dict[str, Any]]:
        with self.workflow.transaction() as conn:
            conn.execute(
                "UPDATE process_management_operation SET phase='running', started_at=? "
                "WHERE operation_id=? AND phase='pending'",
                (_utc_timestamp(), operation_id),
            )
            row = conn.execute("SELECT * FROM process_management_operation WHERE operation_id=?", (operation_id,)).fetchone()
        return self._document(row) if row is not None and row["phase"] == "running" else None

    def progress(self, operation_id: str, progress: Mapping[str, Any]) -> None:
        with self.workflow.transaction() as conn:
            row = conn.execute(
                "SELECT progress FROM process_management_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                return
            merged = {**json.loads(row["progress"] or "{}"), **_plain(dict(progress))}
            conn.execute(
                "UPDATE process_management_operation SET progress=? WHERE operation_id=?", (_dumps(merged), operation_id)
            )

    def finish(
        self,
        operation_id: str,
        *,
        phase: str,
        result: Any,
        error: Optional[dict[str, Any]],
        preview_id: Optional[str] = None,
    ) -> None:
        with self.workflow.transaction() as conn:
            conn.execute(
                "UPDATE process_management_operation SET phase=?, finished_at=?, result=?, error=?, preview_id=? "
                "WHERE operation_id=?",
                (
                    phase,
                    _utc_timestamp(),
                    None if result is None else _dumps(result),
                    None if error is None else _dumps(error),
                    preview_id,
                    operation_id,
                ),
            )

    def find_preview(self, preview_id: str) -> Optional[dict[str, Any]]:
        rows = self._read(
            "SELECT * FROM process_management_operation WHERE preview_id=? AND command='material.layout.preview' "
            "AND phase='succeeded' ORDER BY finished_at DESC LIMIT 1",
            (preview_id,),
        )
        return self._document(rows[0]) if rows else None

    def interrupt_unfinished(self) -> list[str]:
        """权威进程重启后：未完成的操作不重放，标记中断失败并保留挂接的既有事实。"""

        interrupted: list[str] = []
        with self.workflow.transaction() as conn:
            for row in conn.execute(
                "SELECT operation_id, phase, progress FROM process_management_operation "
                "WHERE phase IN ('pending', 'running')"
            ).fetchall():
                details: dict[str, Any] = {"phase": row["phase"]}
                progress = json.loads(row["progress"] or "{}")
                if progress:
                    details["progress"] = progress
                error = ProcessManagementError(
                    "operation_interrupted",
                    "调度权威进程在操作完成前重启；未自动重放，请核对状态后用新的 operationId 重新提交",
                    details=details,
                ).as_dict()
                conn.execute(
                    "UPDATE process_management_operation SET phase='failed', finished_at=?, error=? WHERE operation_id=?",
                    (_utc_timestamp(), _dumps(error), row["operation_id"]),
                )
                interrupted.append(row["operation_id"])
        return interrupted


# ── 参数解析 ────────────────────────────────────────────────────────


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProcessManagementError("invalid_request", f"{field} 必须是非空字符串")
    if field == "operationId" and (
        any(character in value for character in "/\\") or len(value) > OPERATION_ID_MAX_LENGTH
    ):
        raise ProcessManagementError("invalid_request", "operationId 无效")
    return value


def _optional_text(payload: Mapping[str, Any], field: str, default: str = "") -> str:
    value = payload.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProcessManagementError("invalid_request", f"{field} 必须是字符串")
    return value.strip()


def _bool(payload: Mapping[str, Any], field: str, default: Optional[bool]) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise ProcessManagementError("invalid_request", f"{field} 必须是布尔值")
    return value


def _timeout(payload: Mapping[str, Any], default: float) -> float:
    value = payload.get("timeoutSeconds", default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= MAX_TIMEOUT_S:
        raise ProcessManagementError("invalid_request", f"timeoutSeconds 必须在 (0, {MAX_TIMEOUT_S:.0f}] 秒内")
    return float(value)


def request_hash(operation_id: str, command: str, parameters: Mapping[str, Any], expected: Optional[int]) -> str:
    """V1 同算法：规范化请求（含 expectedRevision）的 sha256。"""

    normalized = {
        "operationId": operation_id,
        "command": command,
        "parameters": parameters,
        "expectedRevision": expected,
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ── 服务 ────────────────────────────────────────────────────────────


class ProcessManagementService:
    """V1 进程管理协议的快照 / 操作 / 日志；所有写操作在一个 FIFO 工作线程里串行执行。"""

    def __init__(
        self,
        workflow: Any,
        *,
        context: Optional[ProcessManagementContext] = None,
        gateway: Optional[HostGateway] = None,
        get_scheduler: Optional[Callable[[], Any]] = None,
        get_execution_backend: Optional[Callable[[], Any]] = None,
        get_edge_control: Optional[Callable[[], Any]] = None,
        get_restart_coordinator: Optional[Callable[[], Any]] = None,
        get_materials: Optional[Callable[[], Any]] = None,
        poll_interval: float = 0.2,
    ) -> None:
        from unilabos.server.backend import composition

        self.store = ProcessManagementStore(workflow)
        self._context = context
        self.gateway = gateway or HostGateway()
        self._get_scheduler = get_scheduler or composition.get_scheduler
        self._get_execution_backend = get_execution_backend or composition.get_execution_backend
        self._get_edge_control = get_edge_control or _default_edge_control
        self._get_restart_coordinator = get_restart_coordinator or _default_restart_coordinator
        self._get_materials = get_materials or composition.get_materials_service
        self.poll_interval = poll_interval
        # 本进程代次：没有控制面权威纪元时（单进程）用它标识 backend 组件
        self._generation = f"authority:{os.getpid()}:{int(time.time() * 1000)}"
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._closed = False
        interrupted = self.store.interrupt_unfinished()
        if interrupted:
            logger.warning("[ProcessManagement] 权威进程重启前未完成的 %d 个工作区操作已标记中断：%s",
                           len(interrupted), ", ".join(interrupted))

    @property
    def context(self) -> ProcessManagementContext:
        return self._context if self._context is not None else process_management_context()

    def close(self) -> None:
        self._closed = True
        with self._worker_lock:
            worker = self._worker
        if worker is not None and worker.is_alive():
            self._queue.put(None)
            worker.join(timeout=5)

    # ── 快照 ──────────────────────────────────────────────────────

    def _scheduler_lifecycle(self) -> Any:
        from unilabos.server.backend.lifecycle import SchedulerLifecycle

        return SchedulerLifecycle(self._get_scheduler, self._get_execution_backend)

    def _drain_status(self) -> Optional[dict[str, Any]]:
        from unilabos.server.backend.lifecycle import SchedulerUnavailable

        try:
            return self._scheduler_lifecycle().status().model_dump()
        except SchedulerUnavailable:
            return None

    def _restart_status(self) -> Optional[dict[str, Any]]:
        try:
            status = self._get_restart_coordinator().status()
        except Exception:  # noqa: BLE001 - 快照不因协调器装配失败而失败
            logger.debug("[ProcessManagement] 读取重启协调器状态失败", exc_info=True)
            return None
        keys = ("pending", "restarting", "mode", "effective_scope", "requested_at", "generation")
        return _plain({key: status.get(key) for key in keys})

    def _topology(self) -> str:
        if not self.gateway.remote():
            return "single-process"
        return "authority+host-child" if self.context.supervisor is not None else "authority"

    def _address(self) -> Optional[str]:
        port = self.context.management_port
        if port is None:
            from unilabos.config.config import BasicConfig

            port = getattr(BasicConfig, "port", None)
        return f"http://127.0.0.1:{int(port)}" if port else None

    def _backend_component(self, drain: Optional[dict[str, Any]], restart: Optional[dict[str, Any]]) -> dict[str, Any]:
        from unilabos.utils.runtime_logs import current_log_path

        edge = self._get_edge_control()
        log_path = current_log_path()
        reasons = (drain or {}).get("pause_reasons") or []
        return {
            "name": "backend",
            "phase": "ready",
            "pid": os.getpid(),
            "address": self._address(),
            "generation": getattr(edge, "authority_epoch", None) or self._generation,
            "logPath": str(log_path) if log_path else None,
            "diagnostic": f"派发已暂停：{', '.join(reasons)}" if reasons else None,
            "capabilities": list(BACKEND_CAPABILITIES),
            "metadata": {
                "role": "authority",
                "resident": True,
                "scheduler": drain,
                "restart": restart,
                "databaseRoot": self.context.database_root or None,
            },
        }

    def _edge_component(self, restart: Optional[dict[str, Any]]) -> tuple[dict[str, Any], Optional[list[dict[str, Any]]]]:
        """Host 组件与它的受管进程列表（Host 不可达时为 None）。"""

        edge = self._get_edge_control()
        metadata: dict[str, Any] = {"role": "host"}
        diagnostic: Optional[str] = None
        processes: Optional[list[dict[str, Any]]] = None
        if not self.gateway.remote():
            execution = self._get_execution_backend()
            phase = "ready" if execution is not None else "idle"
            pid: Optional[int] = os.getpid()
            generation: Optional[str] = self._generation
            metadata.update(inProcess=True, supervised=False)
            if execution is None:
                diagnostic = "本进程没有设备执行面"
        else:
            connected = bool(edge is not None and edge.connected)
            generation = (getattr(edge, "edge_process", "") or None) if edge is not None else None
            supervisor = self.context.supervisor
            metadata.update(
                inProcess=False,
                connected=connected,
                transport="runtime.v1 control WebSocket（Host 不监听端口）",
                databaseRoot=self.context.edge_database_root or None,
            )
            if supervisor is not None:
                state = supervisor.state()
                pid = state.get("pid")
                metadata.update(supervised=True, **{
                    "restartCount": state.get("restart_count", 0),
                    "crashCount": state.get("crash_count", 0),
                    "lastExitCode": state.get("last_exit_code"),
                })
                if state.get("alive"):
                    phase = "ready" if connected else "starting"
                    if not connected:
                        diagnostic = "Host 子进程已拉起，等待接入控制面"
                elif state.get("supervising"):
                    phase = "failed" if state.get("crash_count") else "starting"
                    if state.get("crash_count"):
                        diagnostic = f"Host 子进程异常退出（code={state.get('last_exit_code')}），退避后自动重启"
                elif state.get("stop_requested"):
                    phase = "idle"
                else:
                    phase = "failed"
                    diagnostic = f"Host 子进程已退出且不再看护（code={state.get('last_exit_code')}）"
            else:
                pid = None
                metadata["supervised"] = False
                phase = "ready" if connected else "idle"
                if not connected:
                    diagnostic = "远端 Host 未接入控制面（由其自身进程或薄监督进程管理）"
            if restart and (restart.get("pending") or restart.get("restarting")):
                metadata["restartPending"] = True
                diagnostic = diagnostic or "安静点重启已登记，等待执行端安静后重启 Host"
            if not connected:
                return self._component("edge", phase, pid, generation, diagnostic, metadata), None
        try:
            processes = self.gateway.list_processes()
        except ProcessManagementError as exc:
            metadata["managedProcesses"] = exc.as_dict()
        component = self._component("edge", phase, pid, generation, diagnostic, metadata)
        if phase == "ready":
            component["capabilities"] = ["device-control"]
        return component, processes

    @staticmethod
    def _component(
        name: str,
        phase: str,
        pid: Optional[int],
        generation: Optional[str],
        diagnostic: Optional[str],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "name": name,
            "phase": phase,
            "pid": pid,
            "address": None,
            "generation": generation,
            "logPath": None,
            "diagnostic": diagnostic,
            "capabilities": [],
            "metadata": metadata,
        }

    @staticmethod
    def _managed_component(process: Mapping[str, Any]) -> dict[str, Any]:
        status = str(process.get("status") or "stopped")
        phase = {
            "running": "ready",
            "starting": "starting",
            "restarting": "starting",
            "crashed": "failed",
            "stopped": "idle",
        }.get(status, status)
        notices = process.get("restart_notices") or []
        diagnostic = process.get("last_error") or (
            "驱动源码已变化，需重启该进程" if process.get("restart_required") else None
        )
        started = process.get("started_at_ms")
        return {
            "name": f"{MANAGED_COMPONENT_PREFIX}{process.get('id')}",
            "phase": phase,
            "pid": process.get("pid"),
            "address": None,
            "generation": str(started) if started else None,
            "logPath": process.get("log_path") or None,
            "diagnostic": diagnostic,
            "capabilities": ["device-control"] if phase == "ready" else [],
            "metadata": {
                "role": "managed-device-process",
                "processId": process.get("id"),
                "name": process.get("name"),
                "status": status,
                "deviceIds": list(process.get("device_ids") or []),
                "autoStart": process.get("auto_start"),
                "restartPolicy": process.get("restart_policy"),
                "restartCount": process.get("restart_count", 0),
                "lastExitCode": process.get("last_exit_code"),
                "restartRequired": bool(process.get("restart_required")),
                "restartNotices": len(notices),
            },
        }

    @staticmethod
    def _unsupported_component(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "phase": "idle",
            "pid": None,
            "address": None,
            "generation": None,
            "logPath": None,
            "diagnostic": "dev 不运行该组件" + ("（PLC 仿真器）" if name == "plc" else "（前端是独立静态站）"),
            "capabilities": [],
            "metadata": {"supported": False},
        }

    def _observe(self) -> tuple[dict[str, Any], str]:
        drain = self._drain_status()
        restart = self._restart_status()
        backend = self._backend_component(drain, restart)
        edge, processes = self._edge_component(restart)
        components: dict[str, Any] = {
            "backend": backend,
            "edge": edge,
            "plc": self._unsupported_component("plc"),
            "renderer": self._unsupported_component("renderer"),
        }
        for process in processes or []:
            component = self._managed_component(process)
            components[component["name"]] = component
        fingerprint = hashlib.sha256(_dumps({
            "components": sorted(
                (name, item["phase"], item["pid"], item["generation"]) for name, item in components.items()
            ),
            "scheduler": (drain or {}).get("phase"),
            "pauseReasons": (drain or {}).get("pause_reasons"),
            "restart": [bool((restart or {}).get("pending")), bool((restart or {}).get("restarting"))],
        }).encode("utf-8")).hexdigest()
        return components, fingerprint

    def snapshot(self) -> dict[str, Any]:
        from unilabos.config.control_token import token_path

        components, fingerprint = self._observe()
        revision = self.store.observe(fingerprint)
        working_dir = _working_dir()
        token_file = token_path(working_dir)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "revision": revision,
            "eventCursor": revision,
            "workspacePath": str(working_dir),
            "host": {
                # V1 的 Workspace Host 是监督者；dev 的监督者就是常驻的调度权威进程
                "phase": "ready",
                "pid": os.getpid(),
                "endpoint": self._address(),
                "tokenPath": str(token_file) if token_file.is_file() else None,
                "platform": platform.system().lower(),
            },
            "configuration": {
                "topology": self._topology(),
                "managementPort": self.context.management_port,
                "databaseRoot": self.context.database_root or None,
                "edgeDatabaseRoot": self.context.edge_database_root or None,
                "commands": list(SUPPORTED_COMMANDS),
            },
            "components": components,
            "updatedAt": _utc_timestamp(),
        }

    # ── 操作 ──────────────────────────────────────────────────────

    def submit(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProcessManagementError("invalid_request", "操作请求必须是 JSON object")
        operation_id = _required_text(payload, "operationId")
        command = _required_text(payload, "command")
        parameters = payload.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise ProcessManagementError("invalid_request", "parameters 必须是 object")
        expected = payload.get("expectedRevision")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            raise ProcessManagementError("invalid_request", "expectedRevision 必须是整数")
        fingerprint = None
        if expected is not None and self.store.get(operation_id) is None:
            # 比较前先观测一次进程状态：Host 崩溃 / 受管进程重启都必须让旧修订号失效
            _, fingerprint = self._observe()
        document, created = self.store.submit(
            operation_id=operation_id,
            command=command,
            parameters=parameters,
            expected_revision=expected,
            request_hash=request_hash(operation_id, command, parameters, expected),
            fingerprint=fingerprint,
        )
        if created:
            self._ensure_worker()
            self._queue.put(operation_id)
        return document

    def operation(self, operation_id: str) -> dict[str, Any]:
        if not operation_id or "/" in operation_id or "\\" in operation_id:
            raise ProcessManagementError("invalid_request", "operationId 无效")
        document = self.store.get(operation_id)
        if document is None:
            raise ProcessManagementError("operation_not_found", f"操作不存在：{operation_id}")
        return document

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="ProcessManagementOperations", daemon=True
                )
                self._worker.start()

    def _run(self) -> None:
        # 唯一的变更通道：保持提交顺序，避免两个调用方同时启停同一组件
        while True:
            operation_id = self._queue.get()
            try:
                if operation_id is None:
                    return
                self._execute(operation_id)
            except Exception:  # noqa: BLE001 - 工作线程不能因单个操作退出
                logger.exception("[ProcessManagement] 执行工作区操作失败：%s", operation_id)
            finally:
                self._queue.task_done()

    def _execute(self, operation_id: str) -> None:
        document = self.store.start(operation_id)
        if document is None:
            return
        command = str(document["command"])
        parameters = dict(document.get("parameters") or {})
        result: Any = None
        error: Optional[dict[str, Any]] = None
        try:
            result = _plain(self._dispatch(operation_id, command, parameters))
            phase = "succeeded"
        except ProcessManagementError as exc:
            phase, error = "failed", exc.as_dict()
        except Exception as exc:  # noqa: BLE001 - 操作边界统一归一化
            logger.exception("[ProcessManagement] 工作区操作 %s（%s）异常", operation_id, command)
            phase, error = "failed", {
                "code": "internal_error",
                "message": str(exc) or type(exc).__name__,
                "traceback": traceback.format_exc(limit=20),
            }
        preview_id = None
        if phase == "succeeded" and command not in READ_ONLY_COMMANDS:
            revision = self.store.bump()
            if command == "material.layout.apply" and isinstance(result, dict):
                result["authoringRevision"] = revision
        if phase == "succeeded" and command == "material.layout.preview" and isinstance(result, dict):
            preview_id = str(result.get("previewId") or "") or None
            result.setdefault("previewArtifact", {})["operationId"] = operation_id
        self.store.finish(operation_id, phase=phase, result=result, error=error, preview_id=preview_id)

    def _dispatch(self, operation_id: str, command: str, parameters: dict[str, Any]) -> Any:
        """在串行边界内执行一个已持久化的命令；失败抛 ``ProcessManagementError``。"""

        if command in UNSUPPORTED_V1_COMMANDS:
            raise ProcessManagementError(
                "command_unsupported", UNSUPPORTED_V1_COMMANDS[command], details={"command": command}
            )
        if command in {"workspace.start", "os.start"}:
            return self._start_host(parameters, resume=command == "workspace.start")
        if command in {"workspace.stop", "os.stop"}:
            return self._stop_host(parameters)
        if command in {"workspace.restart", "os.restart"}:
            return self._restart_host(operation_id, parameters)
        if command == "backend.start":
            # 调度权威就是提供本接口的进程，必然在运行
            return self.snapshot()
        if command in {"backend.stop", "backend.restart"}:
            raise ProcessManagementError(
                "authority_resident",
                "调度权威进程常驻，不能经自身管理接口停止或重启；重启 Host 用 os.restart，停机请结束 unilab 进程",
            )
        if command == "scheduler.drain":
            return self._drain(_timeout(parameters, DEFAULT_DRAIN_TIMEOUT_S))
        if command == "scheduler.resume":
            return self._resume()
        if command.startswith("device-process."):
            return self._device_process(command.removeprefix("device-process."), parameters)
        if command.startswith("driver-package."):
            return self._driver_package(operation_id, command.removeprefix("driver-package."), parameters)
        if command.startswith("material.layout."):
            return self._layout(operation_id, command.removeprefix("material.layout."), parameters)
        raise ProcessManagementError("command_unknown", f"未知进程管理命令：{command}")

    # Host 子进程启停 / 重启

    def _supervisor(self) -> Any:
        supervisor = self.context.supervisor
        if supervisor is None:
            raise ProcessManagementError(
                "host_not_supervised",
                "本进程没有看护 Host 子进程（--role backend 的远端 Host 或 --no-safe-restart 单进程），不能启停 Host",
            )
        return supervisor

    def _start_host(self, parameters: Mapping[str, Any], *, resume: bool) -> dict[str, Any]:
        supervisor = self._supervisor()
        timeout = _timeout(parameters, DEFAULT_READY_TIMEOUT_S)
        edge = self._get_edge_control()
        state = supervisor.state()
        previous = (getattr(edge, "edge_process", "") or "") if edge is not None else ""
        started = False
        if not (state.get("alive") or state.get("supervising")):
            supervisor.start()
            started = True
        deadline = time.monotonic() + timeout
        while True:
            connected = bool(edge is not None and edge.connected)
            current = (getattr(edge, "edge_process", "") or "") if edge is not None else ""
            if connected and (not started or not previous or current != previous):
                break
            if time.monotonic() >= deadline:
                raise ProcessManagementError(
                    "os_readiness_failed",
                    "等待 Host 接入控制面超时；Host 仍由权威进程看护，可稍后查询快照",
                    details={"timeoutSeconds": timeout, "supervisor": _plain(supervisor.state())},
                )
            time.sleep(self.poll_interval)
        snapshot = self.snapshot()
        if resume:
            resumed = self._resume()
            snapshot["drain"] = resumed
        return snapshot

    def _stop_host(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        supervisor = self._supervisor()
        # 先排空再停：排空超时保持 Host 运行与排空闸门（V1 workspace_drain_timeout 同口径）
        drain = self._drain(_timeout(parameters, DEFAULT_DRAIN_TIMEOUT_S))
        supervisor.stop()
        snapshot = self.snapshot()
        snapshot["drain"] = drain
        return snapshot

    def _restart_host(self, operation_id: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if self._get_edge_control() is None:
            raise ProcessManagementError(
                "host_restart_unsupported",
                "本进程不是带控制面的调度权威（单进程形态），Host 重启请用 POST /api/v1/restart",
            )
        mode = _optional_text(parameters, "mode", "quiescent") or "quiescent"
        if mode not in {"quiescent", "immediate"}:
            raise ProcessManagementError("invalid_request", "mode 必须是 quiescent 或 immediate")
        timeout = _timeout(parameters, DEFAULT_RESTART_TIMEOUT_S)
        coordinator = self._get_restart_coordinator()
        before = coordinator.outcome()
        before_sequence = int(before["sequence"]) if before else 0
        try:
            status = coordinator.request(mode=mode, scope="edge")
        except ValueError as exc:
            raise ProcessManagementError("restart_rejected", str(exc)) from exc
        generation = status.get("generation")
        self.store.progress(operation_id, {"restart": {"generation": generation, "mode": mode}})
        deadline = time.monotonic() + timeout
        while True:
            outcome = coordinator.outcome()
            if outcome and int(outcome["sequence"]) > before_sequence and outcome.get("generation") == generation:
                if outcome["outcome"] == "succeeded":
                    snapshot = self.snapshot()
                    snapshot["restart"] = _plain(outcome)
                    return snapshot
                if outcome["outcome"] == "canceled":
                    raise ProcessManagementError(
                        "restart_canceled", "重启请求已被取消（DELETE /api/v1/restart）", details=_plain(outcome)
                    )
                raise ProcessManagementError(
                    "restart_failed", str(outcome.get("reason") or "Host 重启失败"), details=_plain(outcome)
                )
            if time.monotonic() >= deadline:
                raise ProcessManagementError(
                    "restart_timeout",
                    "Host 重启未在预算内完成；重启请求仍在协调器中，可用 DELETE /api/v1/restart 取消",
                    details=_plain(coordinator.status()),
                )
            time.sleep(self.poll_interval)

    # 排空 / 恢复

    def _drain(self, timeout: float) -> dict[str, Any]:
        from unilabos.server.backend.lifecycle import SchedulerUnavailable

        lifecycle = self._scheduler_lifecycle()
        try:
            status = lifecycle.begin_drain()
        except SchedulerUnavailable as exc:
            raise ProcessManagementError("workspace_drain_unavailable", str(exc)) from exc
        deadline = time.monotonic() + timeout
        while status.phase != "drained":
            if time.monotonic() >= deadline:
                raise ProcessManagementError(
                    "workspace_drain_timeout",
                    "设备作业未在排空预算内收敛；进程保持运行，排空闸门保留",
                    details=status.model_dump(),
                )
            time.sleep(self.poll_interval)
            status = lifecycle.status()
        return status.model_dump()

    def _resume(self) -> dict[str, Any]:
        from unilabos.server.backend.lifecycle import SchedulerUnavailable

        try:
            return self._scheduler_lifecycle().resume().model_dump()
        except SchedulerUnavailable as exc:
            raise ProcessManagementError("workspace_resume_failed", str(exc)) from exc

    # 受管设备进程

    def _device_process(self, action: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ProcessManagementError("command_unknown", f"未知进程管理命令：device-process.{action}")
        process_id = _required_text(parameters, "processId")
        if not MANAGED_PROCESS_ID.fullmatch(process_id):
            raise ProcessManagementError("invalid_request", "processId 无效")
        return {"process": self.gateway.process_action(process_id, action)}

    # 驱动包：沿用 Host 的操作台账，本记录只挂接其 operation_id

    def _driver_package(self, operation_id: str, action: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if action == "enable":
            name = _required_text(parameters, "name")
            return {"package": self.gateway.set_package_enabled(name, _bool(parameters, "enabled", None))}
        if action == "install":
            accepted = self.gateway.install_package(
                _required_text(parameters, "spec"),
                _bool(parameters, "enable", True),
                _bool(parameters, "upgrade", False),
                _optional_text(parameters, "name"),
            )
        elif action == "uninstall":
            accepted = self.gateway.uninstall_package(_required_text(parameters, "name"))
        else:
            raise ProcessManagementError("command_unknown", f"未知进程管理命令：driver-package.{action}")
        timeout = _timeout(parameters, DEFAULT_PACKAGE_TIMEOUT_S)
        package_operation_id = str(accepted.get("operation_id") or "")
        if not package_operation_id:
            raise ProcessManagementError("host_protocol_invalid", "驱动包操作未返回 operation_id")
        self.store.progress(operation_id, {"packageOperationId": package_operation_id})
        deadline = time.monotonic() + timeout
        record = accepted
        while record.get("status") == "running":
            if time.monotonic() >= deadline:
                raise ProcessManagementError(
                    "driver_package_timeout",
                    "驱动包操作未在预算内结束；可经 /api/v1/driver-packages/operations 继续跟踪",
                    details={"packageOperationId": package_operation_id},
                )
            time.sleep(self.poll_interval)
            record = self.gateway.package_operation(package_operation_id)
        if record.get("status") != "succeeded":
            raise ProcessManagementError(
                "driver_package_failed",
                str(record.get("error") or f"驱动包操作结束于 {record.get('status')}"),
                details={"packageOperation": record},
            )
        return {"packageOperation": record}

    # 物料布局

    def _layout(self, operation_id: str, action: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        from unilabos.server.services.materials.workspace_layout import MaterialLayout

        root = _optional_text(parameters, "rootMaterialUuid") or None
        if action == "inspect":
            return MaterialLayout(self._get_materials(), root_material_uuid=root, workspace=_working_dir()).inspect()
        if action == "preview":
            return MaterialLayout(self._get_materials(), root_material_uuid=root, workspace=_working_dir()).preview(
                parameters.get("changeSet"),
                expected_revision=_required_text(parameters, "expectedRevision"),
            )
        if action == "apply":
            preview_id = _required_text(parameters, "previewId")
            if not _PREVIEW_ID.fullmatch(preview_id):
                raise ProcessManagementError("layout_preview_invalid", "preview identity 无效")
            stored = self.store.find_preview(preview_id)
            if stored is None or not isinstance(stored.get("result"), dict):
                raise ProcessManagementError("layout_preview_not_found", f"布局 preview 不存在：{preview_id}")
            preview = stored["result"]
            layout = MaterialLayout(
                self._get_materials(), root_material_uuid=preview.get("rootMaterialUuid"), workspace=_working_dir()
            )
            return layout.apply(
                preview,
                expected_revision=_required_text(parameters, "expectedRevision"),
                command_uuid=f"process-management:{operation_id}",
            )
        raise ProcessManagementError("command_unknown", f"未知进程管理命令：material.layout.{action}")

    # ── 日志 ──────────────────────────────────────────────────────

    def log_tail(self, component: str, max_bytes: int) -> dict[str, Any]:
        """有界读取组件日志尾部；文件只由应用选择（本进程主日志 / Host 日志来源），不接受路径。"""

        from unilabos.protocol.runtime.logs import RuntimeLogBatch
        from unilabos.utils.runtime_logs import current_log_path, read_log_file

        if isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_LOG_MAX_BYTES:
            raise ProcessManagementError("invalid_request", "maxBytes 超出范围")
        if component in UNSUPPORTED_COMPONENTS:
            return {"component": component, "logPath": None, "content": ""}
        source_id: Optional[str] = None
        if component == "backend" or (component == "edge" and not self.gateway.remote()):
            path = current_log_path()
            if path is None or not path.is_file():
                return {"component": component, "logPath": None, "content": ""}
            batch = read_log_file(path, source_id=component, limit=LOG_READ_LINES, pid=os.getpid())
        elif component == "edge" or component.startswith(MANAGED_COMPONENT_PREFIX):
            if component.startswith(MANAGED_COMPONENT_PREFIX):
                process_id = component.removeprefix(MANAGED_COMPONENT_PREFIX)
                if not MANAGED_PROCESS_ID.fullmatch(process_id):
                    raise ProcessManagementError("component_invalid", f"未知组件：{component}")
                source_id = component
            else:
                source_id = "host"
            try:
                batch = RuntimeLogBatch.model_validate(self.gateway.read_log(source_id))
            except ProcessManagementError as exc:
                if exc.code == "host_resource_not_found":
                    raise ProcessManagementError("component_invalid", f"未知组件：{component}") from exc
                raise ProcessManagementError(
                    "component_unavailable", f"组件日志暂不可读：{exc.message}", details=exc.as_dict()
                ) from exc
        else:
            raise ProcessManagementError("component_invalid", f"未知组件：{component}")
        text = "".join(f"{line.text}\n" for line in batch.lines)
        encoded = text.encode("utf-8")
        cut = len(encoded) > max_bytes
        if cut:
            encoded = encoded[-max_bytes:]
            # 从字节尾部截取可能落在多字节字符中间：丢掉开头的续字节
            while encoded and (encoded[0] & 0xC0) == 0x80:
                encoded = encoded[1:]
        return {
            "component": component,
            "logPath": batch.path or None,
            "content": encoded.decode("utf-8", errors="replace"),
            # dev 扩展：读取端单次上限 256 KiB / 1000 行，超过或按 maxBytes 截断时为 true
            "truncated": bool(cut or batch.truncated or batch.has_more),
            "sourceId": source_id or component,
        }


def _default_edge_control() -> Any:
    from unilabos.server.backend.edge_control import get_edge_control_service

    return get_edge_control_service()


def _default_restart_coordinator() -> Any:
    from unilabos.server.backend.restart import get_restart_coordinator

    return get_restart_coordinator()


__all__ = [
    "HostGateway",
    "ProcessManagementContext",
    "ProcessManagementService",
    "ProcessManagementStore",
    "configure_process_management",
    "request_hash",
    "process_management_context",
]
