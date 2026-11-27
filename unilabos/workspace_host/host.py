"""Per-workspace lifecycle authority exposed through an authenticated loopback API."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from unilabos.app.edge_control.addressing import (
    normalize_scheduler_address,
    resolve_scheduler_address,
)
from unilabos.utils.log_storage import LogPolicy, read_log_tail
from unilabos.utils.process_output import ProcessOutput

from .discovery import WorkspaceHostLock, ensure_local_token
from .launch import (
    LaunchPlan,
    available_loopback_port,
    resolve_backend_launch,
    resolve_edge_launch,
    resolve_plc_launch,
    resolve_workspace_graph,
)
from .model import (
    COMPONENT_NAMES,
    DATABASE_SELECTION_CAPABILITY,
    SCHEMA_VERSION,
    STATION_FILE_CAPABILITY,
    WorkspaceHostError,
    WorkspacePaths,
    atomic_write_json,
    idle_component,
    read_json,
    utc_timestamp,
)
from .reset_safety import LocalResetInspectionError, inspect_local_reset_blockers
from .scheduler_lifecycle import SchedulerLifecycleClient, SchedulerLifecycleError

_STOP_TIMEOUT_SECONDS = 10.0
# 工作区更新可能触发一次完整工作流依赖激活。Backend 会很早开放 health，但
# Workspace Host 只把 readiness 当成完整可用；统一总预算避免每个探针各等一轮。
_READINESS_TIMEOUT_SECONDS = 600.0
# Backend 与 Edge 各自最多使用一轮 readiness 预算。恢复派发只是一次本地 HTTP
# 请求，必须保持短预算，确保整个 workspace.start 在容器 PID 1 的 1250 秒等待
# 窗口内结束；否则客户端超时后串行 workspace.stop 仍会排在未完成的 start 后面。
_SCHEDULER_RESUME_TIMEOUT_SECONDS = 10.0
_MAX_BODY_BYTES = 1024 * 1024
_SUPERVISED_COMPONENTS = frozenset({"backend", "edge", "plc"})
_LOCAL_RESET_AFTER_STOP_INSPECTION_ATTEMPTS = 20
_LOCAL_RESET_INSPECTION_RETRY_DELAY_SECONDS = 0.1


class WorkspaceHost:
    """Own component processes, operations, state revision, logs, and audit."""

    def __init__(
        self,
        paths: WorkspacePaths,
        token: str,
        *,
        readiness_timeout: float = _READINESS_TIMEOUT_SECONDS,
        backend_port: int | None = None,
    ) -> None:
        """初始化工作区唯一的进程所有者并恢复持久化组件状态。

        参数：``paths`` 是工作区标准路径，``token`` 是本地 Host 鉴权令牌，
        ``readiness_timeout`` 是组件业务就绪总预算，``backend_port`` 是容器部署
        可选的固定 Backend 监听端口。返回：无；实例启动串行操作线程并准备组件
        监控。异常：持久化状态无效、路径不可访问或恢复失败时透传
        ``WorkspaceHostError`` 或相应系统异常。
        """

        self.paths = paths
        self.token = token
        self.readiness_timeout = readiness_timeout
        self._backend_port = backend_port
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._operation_queue: queue.Queue[str | None] = queue.Queue()
        self._operation_worker = threading.Thread(
            target=self._run_operations,
            name="unilab-workspace-host-operations",
            daemon=True,
        )
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._recovery_pending: dict[str, float] = {}
        self._recovery_attempts: dict[str, int] = {}
        self._revision = 0
        self._cursor = 0
        self._endpoint = ""
        self._configuration = self._initial_configuration()
        self._components = {name: idle_component(name) for name in COMPONENT_NAMES}
        self._scheduler_lifecycle = SchedulerLifecycleClient()
        self._restore_interrupted_components()
        self._closed = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_processes,
            name="unilab-workspace-host-monitor",
            daemon=True,
        )
        self._operation_worker.start()

    def publish_endpoint(self, endpoint: str) -> None:
        with self._lock:
            self._endpoint = endpoint
            self._publish_locked("host.ready", {"endpoint": endpoint})
        self._monitor.start()

    def authorized(self, authorization: str | None) -> bool:
        expected = f"Bearer {self.token}"
        return authorization is not None and hmac.compare_digest(
            authorization,
            expected,
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._refresh_processes_locked()
            return self._snapshot_locked()

    def submit(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise WorkspaceHostError("invalid_request", "操作请求必须是 JSON object")
        operation_id = _required_text(payload, "operationId")
        command = _required_text(payload, "command")
        parameters = payload.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise WorkspaceHostError("invalid_request", "parameters 必须是 object")
        expected_revision = payload.get("expectedRevision")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
        ):
            raise WorkspaceHostError("invalid_request", "expectedRevision 必须是整数")
        normalized = {
            "operationId": operation_id,
            "command": command,
            "parameters": parameters,
            "expectedRevision": expected_revision,
        }
        request_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        operation_path = self.paths.operations / f"{operation_id}.json"
        with self._lock:
            if operation_path.exists():
                existing = read_json(operation_path)
                if existing.get("requestHash") != request_hash:
                    raise WorkspaceHostError(
                        "operation_conflict",
                        f"operationId 已被不同请求使用：{operation_id}",
                    )
                return _public_database_parameters(existing)
            if expected_revision is not None and expected_revision != self._revision:
                raise WorkspaceHostError(
                    "revision_conflict",
                    "Workspace Host revision 已变化",
                    details={"expected": expected_revision, "actual": self._revision},
                )
            operation: dict[str, object] = {
                "schemaVersion": SCHEMA_VERSION,
                "operationId": operation_id,
                "command": command,
                "parameters": parameters,
                "requestHash": request_hash,
                "phase": "pending",
                "submittedAt": utc_timestamp(),
                "startedAt": None,
                "finishedAt": None,
                "result": None,
                "error": None,
            }
            atomic_write_json(operation_path, operation)
            self._audit_locked("operation.submitted", operation)
        # One FIFO worker is the mutation lane shared by CLI, Workbench and
        # future MCP clients. It preserves submission order and prevents two
        # callers from spawning or stopping the same component concurrently.
        self._operation_queue.put(operation_id)
        return _public_database_parameters(operation)

    def operation(self, operation_id: str) -> dict[str, object]:
        if not operation_id or "/" in operation_id or "\\" in operation_id:
            raise WorkspaceHostError("invalid_request", "operationId 无效")
        path = self.paths.operations / f"{operation_id}.json"
        try:
            return _public_database_parameters(read_json(path))
        except WorkspaceHostError as error:
            if error.code == "host_not_found":
                raise WorkspaceHostError(
                    "operation_not_found",
                    f"操作不存在：{operation_id}",
                ) from error
            raise

    def log_tail(self, component: str, max_bytes: int) -> dict[str, object]:
        if component not in COMPONENT_NAMES:
            raise WorkspaceHostError("component_invalid", f"未知组件：{component}")
        if max_bytes < 1 or max_bytes > 4 * 1024 * 1024:
            raise WorkspaceHostError("invalid_request", "maxBytes 超出范围")
        with self._lock:
            value = self._components[component].get("logPath")
        if not isinstance(value, str) or not value:
            return {"component": component, "logPath": None, "content": ""}
        path = Path(value)
        content = read_log_tail(path, max_bytes).decode("utf-8", errors="replace")
        return {"component": component, "logPath": str(path), "content": content}

    def close(self) -> None:
        """Stop only Host bookkeeping; managed components intentionally survive."""

        self._closed.set()
        self._operation_queue.put(None)
        if self._operation_worker.is_alive():
            self._operation_worker.join(timeout=1.0)
        if self._monitor.is_alive():
            self._monitor.join(timeout=1.0)

    def _run_operations(self) -> None:
        while True:
            operation_id = self._operation_queue.get()
            try:
                if operation_id is None:
                    return
                self._execute_operation(operation_id)
            finally:
                self._operation_queue.task_done()

    def _execute_operation(self, operation_id: str) -> None:
        operation_path = self.paths.operations / f"{operation_id}.json"
        with self._lock:
            operation = read_json(operation_path)
            operation["phase"] = "running"
            operation["startedAt"] = utc_timestamp()
            atomic_write_json(operation_path, operation)
            self._audit_locked("operation.started", operation)
        try:
            with self._lifecycle_lock:
                result = self._dispatch(
                    str(operation["command"]),
                    dict(operation.get("parameters") or {}),
                )
        except WorkspaceHostError as error:
            phase = "failed"
            result = None
            failure: object = error.as_dict()
        except BaseException as error:  # noqa: BLE001 - operation boundary normalizes.
            phase = "failed"
            result = None
            failure = {
                "code": "internal_error",
                "message": str(error),
                "traceback": traceback.format_exc(limit=20),
            }
        else:
            phase = "succeeded"
            failure = None
        with self._lock:
            operation = read_json(operation_path)
            operation["phase"] = phase
            operation["finishedAt"] = utc_timestamp()
            operation["result"] = result
            operation["error"] = failure
            atomic_write_json(operation_path, operation)
            self._audit_locked(f"operation.{phase}", operation)

    def _dispatch(self, command: str, parameters: dict[str, object]) -> object:
        """在串行生命周期边界内执行一个已持久化操作。

        参数：``command`` 是 Host 公开命令，``parameters`` 是已校验 JSON 参数。
        返回：命令结果或工作区快照。异常：未知命令、状态冲突和外部依赖失败均
        抛 ``WorkspaceHostError``；调用者统一写入操作终态和审计记录。
        """

        if "stationFile" in parameters:
            from .station_config import StationFile, StationLaunch

            if command not in {"workspace.start", "workspace.restart"}:
                raise WorkspaceHostError("invalid_request", "工站启动文件只支持整站 start/restart")
            station = StationFile.load(_required_text(parameters, "stationFile"))
            if station.digest != parameters.get("stationFileDigest"):
                raise WorkspaceHostError("station_config_changed", "启动文件已修改，请重新提交")
            launch = StationLaunch.prepare(station, self.paths, parameters)
            with self._lock:
                backend = self._components["backend"]
                if (command == "workspace.start" and backend["phase"] == "ready"
                        and backend.get("metadata", {}).get("stationStartSignature") != launch.signature()):
                    raise WorkspaceHostError("station_restart_required", "工作区已启动；请使用 workspace restart -f 应用工站配置")
            parameters = {**parameters, "_stationLaunch": launch}
        if command in {
            "workspace.start", "workspace.restart", "backend.start", "backend.restart",
            "os.start", "os.restart",
        }:
            self._select_database(command, parameters)
        if command == "workspace.start":
            return self._start_workspace(parameters)
        if command == "workspace.stop":
            return self._stop_workspace()
        if command == "workspace.restart":
            self._stop_workspace()
            self._select_database("workspace.start", parameters)
            return self._start_workspace(parameters)
        if command == "backend.start":
            return self._start_backend(parameters)
        if command == "backend.stop":
            return self._stop_component("backend")
        if command == "backend.restart":
            with self._lock:
                edge_was_ready = self._components["edge"].get("phase") == "ready"
            if edge_was_ready:
                self._stop_component("edge")
            self._stop_component("backend")
            backend = self._start_backend(parameters)
            return self._start_edge() if edge_was_ready else backend
        if command == "os.start":
            return self._start_edge()
        if command == "os.stop":
            return self._stop_component("edge")
        if command == "os.restart":
            self._stop_component("edge")
            return self._start_edge()
        if command == "local.reset-state":
            return self._reset_local_state(parameters)
        if command == "plc.start":
            return self._start_plc()
        if command == "plc.stop":
            return self._stop_component("plc")
        if command == "plc.restart":
            self._stop_component("plc")
            return self._start_plc()
        if command == "configuration.update":
            return self._update_configuration(parameters)
        if command == "authority.switch":
            raise WorkspaceHostError(
                "backend_mode_removed",
                "当前 OS 固定使用 local 控制面，不能切换到 backend",
            )
        if command == "release.publish":
            raise WorkspaceHostError(
                "backend_mode_removed",
                "当前 OS 不再向 Backend 发布或同步数据",
            )
        if command == "release.inspect":
            raise WorkspaceHostError(
                "backend_mode_removed",
                "当前 OS 不再检查远端 Backend 发布目标",
            )
        if command == "renderer.attach":
            return self._attach_renderer(parameters)
        if command == "renderer.detach":
            return self._detach_renderer(parameters)
        if command == "renderer.headless.ensure":
            return self._ensure_headless_renderer()
        if command == "renderer.headless.stop":
            return self._stop_headless_renderer()
        if command == "material.layout.inspect":
            return self._material_layout().inspect()
        if command == "material.layout.preview":
            return self._material_layout().preview(
                parameters.get("changeSet"),
                expected_revision=_required_text(parameters, "expectedRevision"),
            )
        if command == "material.layout.apply":
            return self._apply_material_layout(parameters)
        if command == "material.template.validate":
            return self._validate_material_templates()
        raise WorkspaceHostError(
            "command_unknown", f"未知 Workspace Host 命令：{command}"
        )

    def _select_database(self, command: str, parameters: dict[str, object]) -> None:
        """固定 Backend、Edge 和 Host 使用的数据库，禁止运行中只切换一个组件。"""
        from unilabos.persistence.config import DatabaseSettings, verify_database_connection
        from unilabos.persistence.workspace import (
            database_selection_path,
            save_workspace_database,
            workspace_settings,
        )

        with self._lock:
            running = any(
                self._components[name].get("phase") not in {"idle", "failed"}
                or (
                    isinstance(self._components[name].get("pid"), int)
                    and _pid_exists(self._components[name]["pid"])
                )
                for name in ("backend", "edge")
            )
        try:
            # 完整 CLI 参数必须能覆盖未启动工作区配置中的无效数据库地址。
            explicit = (
                DatabaseSettings(str(parameters["databaseType"]), str(parameters["databaseUrl"]))
                if "databaseType" in parameters and "databaseUrl" in parameters else None
            )
            try:
                current = workspace_settings(self.paths.workspace)
            except ValueError:
                if running or explicit is None or database_selection_path(self.paths.workspace).exists():
                    raise
                current = explicit
            selected = explicit or DatabaseSettings(
                str(parameters.get("databaseType", current.database_type)),
                str(parameters.get("databaseUrl", current.database_url)),
            )
        except (ValueError, OSError) as error:
            raise WorkspaceHostError("database_configuration_invalid", str(error)) from error
        if selected.database_type == "sqlite":
            selected = DatabaseSettings()
        if current.database_type == "sqlite":
            current = DatabaseSettings()
        if running and explicit is not None and command != "workspace.restart":
            with self._lock:
                unverified = any(
                    self._components[name].get("phase") in {"ready", "interrupted"}
                    and not self._components[name].get("metadata", {}).get("databaseType")
                    for name in ("backend", "edge")
                )
            if unverified:
                raise WorkspaceHostError(
                    "database_change_requires_restart",
                    "旧组件未记录数据库选择；请使用 workspace restart 同时重启 Backend 和 Edge 后再确认数据库",
                )
        if running and selected != current and command != "workspace.restart":
            raise WorkspaceHostError(
                "database_change_requires_restart",
                "工作区正在使用其他数据库；请使用 workspace restart 同时重启 Backend 和 Edge",
            )
        if selected != current or not running or command.endswith(".restart"):
            try:
                verify_database_connection(selected)
            except Exception as error:
                raise WorkspaceHostError(
                    "database_connection_failed", "无法连接所选数据库；未回退到 SQLite",
                ) from error
        # restart 必须先停止使用旧库的全部进程，再发布新选择。
        if command != "workspace.restart":
            save_workspace_database(self.paths.workspace, selected)

    def _material_layout(self):
        """Resolve the selected source graph without coupling it to Edge state."""

        from .material_layout import MaterialLayoutWorkspace

        with self._lock:
            backend = dict(self._components["backend"])
            configuration = dict(self._configuration)
        metadata = backend.get("metadata")
        graph_path = (
            _optional_text(metadata.get("graphPath"))
            if isinstance(metadata, dict)
            else None
        )
        selected_graph = resolve_workspace_graph(
            self.paths, graph_path=graph_path, configuration=configuration
        )
        return MaterialLayoutWorkspace(self.paths, selected_graph)

    def _validate_material_templates(self) -> dict[str, object]:
        """Compile the full workspace catalog in an isolated, disposable process."""

        generation = str(uuid.uuid4())
        runtime = self.paths.runtime / "template-validation" / generation
        runtime.mkdir(parents=True, exist_ok=False)
        result_path = runtime / "result.json"
        log_path = self.paths.logs / f"{generation}-template-validation.log"
        command = [
            sys.executable,
            "-m",
            "unilabos.workspace_host.material_template_worker",
            "--workspace",
            str(self.paths.workspace),
            "--output",
            str(result_path),
        ]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            os.pathsep.join((source_root, existing_python_path))
            if existing_python_path
            else source_root
        )
        try:
            with ProcessOutput(log_path, LogPolicy.from_env(environment)) as output:
                completed = subprocess.run(
                    command,
                    cwd=self.paths.workspace,
                    env=output.environment(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=output.stream,
                    stderr=subprocess.STDOUT,
                    timeout=max(self.readiness_timeout, 120.0),
                    check=False,
                )
            output.wait()
        except subprocess.TimeoutExpired as error:
            raise WorkspaceHostError(
                "template_validation_timeout",
                f"模板隔离编译超时；日志：{log_path}",
            ) from error
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceHostError(
                "template_validation_failed",
                f"模板隔离编译未返回有效结果；日志：{log_path}",
                details={"exitCode": completed.returncode},
            ) from error
        if not isinstance(result, dict):
            raise WorkspaceHostError(
                "template_validation_failed", "模板隔离编译结果必须是 object"
            )
        return {
            **result,
            "generation": generation,
            "isolatedProcess": True,
            "exitCode": completed.returncode,
            "logPath": str(log_path),
            "resultPath": str(result_path),
            "lastValidScenePreserved": True,
        }

    def _apply_material_layout(
        self, parameters: dict[str, object]
    ) -> dict[str, object]:
        """Apply one proven source preview and refresh the local projection."""

        with self._lock:
            domain_mode = str(self._configuration.get("domainMode") or "local")
            backend = dict(self._components["backend"])
        if domain_mode != "local":
            raise WorkspaceHostError(
                "layout_authority_mismatch",
                "backend Authority 下不能把工作区设备布局隐式写入远端；请切回 local",
            )
        result = self._material_layout().apply(
            _required_text(parameters, "previewId"),
            expected_revision=_required_text(parameters, "expectedRevision"),
        )
        live_projection = self._publish_material_layout(backend, result)
        renderer_refresh = self._refresh_attached_material_renderer()
        with self._lock:
            metadata = self._components["backend"].setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["graphFingerprint"] = str(result["revision"]).removeprefix(
                    "sha256:"
                )
                metadata["materialLayoutRevision"] = result["revision"]
            self._publish_locked(
                "material.layout.applied",
                {
                    "previewId": result["previewId"],
                    "revision": result["revision"],
                    "changedSourceNodeIds": result["changedSourceNodeIds"],
                },
            )
        return {
            **result,
            "authoringRevision": self._revision,
            "liveProjection": live_projection,
            "rendererRefresh": renderer_refresh,
        }

    def _publish_material_layout(
        self,
        backend: dict[str, object],
        result: dict[str, object],
    ) -> dict[str, object]:
        """Update the running local Backend through its Backend-shaped API."""

        address = _optional_text(backend.get("address"))
        if backend.get("phase") != "ready" or not address:
            return {"status": "backend-not-ready", "updated": 0}
        payload = self._json_request(f"{address}/api/v1/materials/graph")
        graph_data = payload.get("data") if isinstance(payload, dict) else None
        nodes = graph_data.get("nodes") if isinstance(graph_data, dict) else None
        if not isinstance(nodes, list):
            raise WorkspaceHostError(
                "layout_projection_failed", "Local Backend 未返回物料图节点"
            )
        by_source_id: dict[str, dict[str, object]] = {}
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            material = raw.get("material")
            metadata = material.get("meta_data") if isinstance(material, dict) else None
            source_id = (
                _optional_text(metadata.get("source_node_id"))
                if isinstance(metadata, dict)
                else None
            )
            if source_id:
                by_source_id[source_id] = raw
        change_set = result.get("changeSet")
        changes = change_set.get("nodes") if isinstance(change_set, dict) else None
        if not isinstance(changes, list):
            raise WorkspaceHostError("layout_projection_failed", "布局变更投影无效")
        updated = 0
        for change in changes:
            if not isinstance(change, dict):
                continue
            source_id = str(change.get("sourceNodeId") or "")
            raw = by_source_id.get(source_id)
            material = raw.get("material") if isinstance(raw, dict) else None
            relative_position = (
                dict(raw.get("relative_position") or {})
                if isinstance(raw, dict)
                else {}
            )
            if not isinstance(material, dict) or not relative_position:
                raise WorkspaceHostError(
                    "layout_projection_failed",
                    f"Local Backend 无法解析布局节点：{source_id}",
                )
            if isinstance(change.get("positionMm"), list):
                for key, value in zip(
                    ("position_x", "position_y", "position_z"),
                    change["positionMm"],
                    strict=True,
                ):
                    relative_position[key] = value
            if isinstance(change.get("rotationDegXYZ"), list):
                for key, value in zip(
                    ("rotation_x", "rotation_y", "rotation_z"),
                    change["rotationDegXYZ"],
                    strict=True,
                ):
                    relative_position[key] = value
            config = dict(material.get("config") or {})
            if isinstance(change.get("assetRef"), dict):
                rendering = dict(config.get("rendering") or {})
                rendering["model"] = dict(change["assetRef"])
                config["rendering"] = rendering
            body = {
                "resource_template_uuid": material.get("resource_template_uuid"),
                "parent_uuid": material.get("parent_uuid"),
                "barcode": material.get("barcode") or "",
                "name": material.get("name") or source_id,
                "description": material.get("description"),
                "meta_data": material.get("meta_data") or {},
                "config": config,
                "relative_position": relative_position,
            }
            material_id = _required_text(material, "uuid")
            self._json_request(
                f"{address}/api/v1/materials/{material_id}",
                method="PUT",
                body=body,
            )
            updated += 1
        return {"status": "updated", "updated": updated, "backendUrl": address}

    def _refresh_attached_material_renderer(self) -> dict[str, object]:
        with self._lock:
            renderer = dict(self._components["renderer"])
        metadata = renderer.get("metadata")
        base_url = (
            _optional_text(metadata.get("automationBaseUrl"))
            if isinstance(metadata, dict)
            else None
        )
        if renderer.get("phase") != "ready" or not base_url:
            return {"status": "detached"}
        try:
            response = self._json_request(
                f"{base_url.rstrip('/')}/material/reload",
                method="POST",
                body={},
                authorization=True,
            )
        except WorkspaceHostError as error:
            return {"status": "failed", "error": error.as_dict()}
        return {"status": "refreshed", "response": response.get("result")}

    def _json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: object = None,
        authorization: bool = False,
    ) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authorization:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with urlopen(
                Request(url, data=data, method=method, headers=headers),
                timeout=self.readiness_timeout,
            ) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError, URLError) as error:
            raise WorkspaceHostError(
                "layout_projection_failed", f"布局实时投影请求失败：{error}"
            ) from error
        if not isinstance(payload, dict):
            raise WorkspaceHostError(
                "layout_projection_failed", "布局实时投影响应不是 JSON object"
            )
        if payload.get("code", 0) != 0 or payload.get("ok") is False:
            raise WorkspaceHostError(
                "layout_projection_failed",
                "布局实时投影被目标拒绝",
                details=payload.get("error"),
            )
        return payload

    def _reset_local_edge_protocol_state(self) -> None:
        """Reset transient Edge protocol facts for an explicit local rebuild.

        The durable Edge instance UUID remains stable.  Ordinary ``os.restart``
        never calls this seam, so pending commands and outcomes retain their
        crash-recovery guarantees.
        """

        from unilabos.app.edge_control.store import EdgeControlStore
        from unilabos.persistence.workspace import use_workspace_database

        with use_workspace_database(self.paths.workspace):
            edge_state_directory = self.paths.runtime / "edge"
            state_paths = sorted(edge_state_directory.glob("edge_control*.db"))
            local_state_path = edge_state_directory / "edge_control.db"
            if local_state_path not in state_paths:
                state_paths.insert(0, local_state_path)
            for state_path in state_paths:
                store = EdgeControlStore(str(state_path))
                try:
                    store.reset_transient_state()
                finally:
                    store.close()

    def _reset_local_state(self, parameters: dict[str, object]) -> object:
        """Rebuild Local Domain state only after durable facts are quiescent."""

        with self._lock:
            backend_was_ready = self._components["backend"].get("phase") == "ready"
            edge_was_ready = self._components["edge"].get("phase") == "ready"
        self._assert_local_reset_safe("before-stop")
        self._stop_component("edge")
        self._stop_component("backend")
        try:
            self._assert_local_reset_safe("after-stop")
        except WorkspaceHostError:
            if backend_was_ready:
                self._start_backend(parameters)
            if edge_was_ready:
                self._start_edge()
            raise
        self._reset_local_edge_protocol_state()
        self._reset_local_domain_state()
        return self._start_backend(parameters)

    def _assert_local_reset_safe(self, stage: str) -> None:
        max_attempts = (
            _LOCAL_RESET_AFTER_STOP_INSPECTION_ATTEMPTS
            if stage == "after-stop"
            else 1
        )
        blockers = []
        for attempt in range(1, max_attempts + 1):
            try:
                blockers = inspect_local_reset_blockers(self.paths)
                break
            except LocalResetInspectionError as error:
                if attempt < max_attempts:
                    with self._lock:
                        self._audit_locked(
                            "local.reset-state.preflight-retry",
                            {
                                "stage": stage,
                                "attempt": attempt,
                                "maxAttempts": max_attempts,
                                "message": str(error),
                            },
                        )
                    time.sleep(_LOCAL_RESET_INSPECTION_RETRY_DELAY_SECONDS)
                    continue
                with self._lock:
                    self._audit_locked(
                        "local.reset-state.preflight-failed",
                        {"stage": stage, "message": str(error)},
                    )
                raise WorkspaceHostError(
                    "local_reset_state_preflight_failed",
                    "无法证明本地状态可以安全重建；未修改任何持久数据",
                    details={"stage": stage, "message": str(error)},
                ) from error
        if not blockers:
            return
        details = {
            "stage": stage,
            "blockers": [blocker.as_dict() for blocker in blockers],
        }
        with self._lock:
            self._audit_locked("local.reset-state.blocked", details)
        raise WorkspaceHostError(
            "local_reset_state_blocked",
            "存在活动工作流或尚未收敛的 Edge 事实；本地状态未重建",
            details=details,
        )

    def _reset_local_domain_state(self) -> None:
        """Delete only the audited Local Domain databases and their journals."""

        from unilabos.persistence.workspace import reset_postgresql_domain, workspace_settings

        settings = workspace_settings(self.paths.workspace)
        if settings.database_type == "postgresql":
            removed = reset_postgresql_domain(settings)
            with self._lock:
                self._publish_locked("local.domain-state.reset", {"removed": removed})
            return
        state_directory = self.paths.runtime / "backend" / "local-domain"
        removed: list[str] = []
        for database_name in (
            "inventory.db",
            "device_state.db",
            "workflow_history.db",
            "edge_authority.db",
        ):
            for filename in (
                database_name,
                f"{database_name}-wal",
                f"{database_name}-shm",
            ):
                path = state_directory / filename
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                removed.append(filename)
        with self._lock:
            self._publish_locked("local.domain-state.reset", {"removed": removed})

    def _start_backend(self, parameters: dict[str, object]) -> dict[str, object]:
        """启动拥有工站调度权威的工作区后端进程。

        参数：``parameters`` 可覆盖图、执行模式和启动可见范围。返回工作区状态
        快照。异常：启动或就绪探测失败时原样转为 ``WorkspaceHostError``，不启动
        动作执行进程。
        """

        with self._lock:
            if self._components["backend"]["phase"] == "ready":
                return self._snapshot_locked()
            configuration = dict(self._configuration)
        from .station_config import StationLaunch

        launch = parameters.get("_stationLaunch")
        if isinstance(launch, StationLaunch):
            plan = launch.backend_plan(self.paths, host_port=self._backend_port)
        else:
            plan = resolve_backend_launch(
                self.paths,
                graph_path=_optional_text(parameters.get("graphPath"))
                or _optional_text(configuration.get("graphPath")),
                runtime_mode=_optional_text(parameters.get("runtimeMode"))
                or _optional_text(configuration.get("runtimeMode")),
                backend_port=self._backend_port,
            )
        self._spawn(plan)
        package_mounts = self._wait_backend_ready(plan)
        with self._lock:
            self._components["backend"]["phase"] = "ready"
            self._components["backend"]["diagnostic"] = None
            metadata = self._components["backend"].setdefault("metadata", {})
            assert isinstance(metadata, dict)
            metadata["packageMounts"] = package_mounts
            self._components["backend"]["capabilities"] = [
                "authoring",
                "inventory",
                "workflow-run",
                "station-scheduler",
            ]
            self._publish_locked("backend.ready", {"generation": plan.generation})
            return self._snapshot_locked()

    def _start_workspace(self, parameters: dict[str, object]) -> dict[str, object]:
        """用一个公开操作启动 Backend 与 Edge 两个独立进程。

        参数：``parameters`` 是 Backend 启动参数。返回：两个进程均业务就绪且
        调度器已退出排空状态后的工作区快照。异常：任一进程或恢复派发失败时
        原样抛出；本次新建的 Backend 会回滚，既有 Backend 保留用于诊断。
        """

        with self._lock:
            backend_ready = self._components["backend"].get("phase") == "ready"
        backend_started = False
        if not backend_ready:
            self._start_backend(parameters)
            backend_started = True
        try:
            self._start_edge()
            self._resume_local_scheduler()
            return self.snapshot()
        except Exception:
            # Edge 未就绪时不能留下一个只有调度、没有设备执行端的“半启动”工作区。
            # 如果 Backend 本来就是用户单独启动的，则保留它供诊断使用。
            with self._lock:
                edge_active = self._components["edge"].get("phase") != "idle"
            if edge_active:
                self._stop_component("edge")
            if backend_started:
                self._stop_component("backend")
            raise

    def _stop_workspace(self) -> dict[str, object]:
        """排空设备作业后，按 Edge、Backend 顺序停止双进程工作区。

        参数：无。返回：两个进程停止后的快照，异常退出时附带设备状态未知告警。
        异常：进程仍存活而排空失败时抛 ``WorkspaceHostError``；只有排空成功或
        已确认调度进程退出时才继续清理软件进程，不代替设备物理状态确认。
        """

        drain = self._drain_local_scheduler()
        self._stop_component("edge")
        if drain.get("warning"):
            # Edge 清理失败时先保留退出 PID，重试还能核实；完成后清除它，
            # 不向可能被系统重新分配的旧 PID 发送终止信号。
            with self._lock:
                self._components["backend"]["pid"] = None
        result = self._stop_component("backend")
        if drain.get("warning"):
            result["warnings"] = [drain["warning"]]
        return result

    def _drain_local_scheduler(self) -> dict[str, object]:
        """通过 Backend 的公开接口等待本地调度器安全排空。

        参数：无。返回：调度器报告的已排空状态；Backend 已停止时返回本地
        的零作业快照；确认异常退出时返回不可确认状态及告警。异常：存活调度器
        地址缺失或排空未收敛时抛出
        ``WorkspaceHostError``；即使 Edge 已单独停止，只要 Backend 仍在运行也
        必须先排空，避免遗漏持久层中等待物理对账的 running 作业。
        """

        with self._lock:
            backend = self._components["backend"]
            backend_phase = backend.get("phase")
            process = self._processes.get("backend")
            pid = backend.get("pid")
            exited = (process.poll() is not None if process is not None else
                      isinstance(pid, int) and pid > 0 and not _pid_exists(pid))
            if backend_phase != "idle" and exited:
                warning = {
                    "code": "scheduler_exited_before_drain",
                    "message": "调度服务已异常退出，无法确认设备作业状态，请人工核对设备。",
                }
                # 退出事实只允许清理软件进程，不代表设备排空或库存占用可释放。
                self._processes.pop("backend", None)
                self._recovery_pending.pop("backend", None)
                backend["phase"] = "stopping"
                self._publish_locked("workspace.drain_unavailable", warning)
                return {"phase": "unavailable", "warning": warning}
        if backend_phase == "idle":
            return {"phase": "drained", "active_device_job_count": 0}
        address = self._local_scheduler_address(required_for="stop")
        if address is None:
            return {"phase": "drained", "active_device_job_count": 0}
        try:
            status = self._scheduler_lifecycle.begin_and_wait(
                address,
                timeout=self.readiness_timeout,
            )
        except SchedulerLifecycleError as error:
            raise WorkspaceHostError(
                error.code,
                error.message,
                details=error.details,
            ) from error
        with self._lock:
            self._publish_locked("workspace.drained", dict(status))
        return status

    def _resume_local_scheduler(self) -> dict[str, object] | None:
        """在动作执行进程就绪后恢复本地工站调度器派发。

        参数：无；从已就绪 Backend 组件解析本地调度地址。返回：恢复后的公开
        调度状态，Backend 已停止时返回 ``None``。异常：地址缺失、接口不可达或
        响应无效时抛 ``WorkspaceHostError``；单次 HTTP 等待固定使用短预算，
        避免统一启动越过容器生命周期上界。
        """

        address = self._local_scheduler_address(required_for="start")
        if address is None:
            return None
        try:
            status = self._scheduler_lifecycle.resume(
                address,
                timeout=min(
                    self.readiness_timeout,
                    _SCHEDULER_RESUME_TIMEOUT_SECONDS,
                ),
            )
        except SchedulerLifecycleError as error:
            raise WorkspaceHostError(
                "workspace_resume_failed",
                error.message,
                details=error.details,
            ) from error
        with self._lock:
            self._publish_locked("workspace.scheduler-resumed", dict(status))
        return status

    def _local_scheduler_address(self, *, required_for: str) -> str | None:
        """返回当前工作区本地工站调度器地址。

        参数：``required_for`` 仅用于错误诊断，取 ``start`` 或 ``stop``。返回：
        本地调度进程就绪时返回地址，已停止时返回 ``None``。异常：就绪组件缺少
        地址时关闭式失败，避免在未排空工站作业时停止动作执行进程。
        """

        with self._lock:
            backend = dict(self._components["backend"])
        if backend.get("phase") == "idle":
            return None
        address = _optional_text(backend.get("address"))
        if address is None:
            raise WorkspaceHostError(
                "workspace_drain_unavailable",
                "本地 Backend 缺少调度器服务地址",
                details={"operation": required_for},
            )
        return address

    def _start_edge(self) -> dict[str, object]:
        """启动 Edge/驱动进程并等待其连接调度控制面。

        参数：无；使用已就绪 Backend 的地址、领域模式和领域包范围。返回：
        generation 对应就绪文件出现后的工作区快照。异常：Backend 不可用时先
        启动 Backend；Edge 提前退出或超时则停止本次 Edge 进程并关闭式失败。
        """

        with self._lock:
            if self._components["edge"]["phase"] == "ready":
                return self._snapshot_locked()
            backend = dict(self._components["backend"])
            backend_metadata = backend.get("metadata")
            if isinstance(backend_metadata, dict):
                backend["metadata"] = {
                    **backend_metadata,
                    "schedulerUrl": self._configuration.get("schedulerUrl"),
                }
        if backend.get("phase") != "ready":
            self._start_backend({})
            with self._lock:
                backend = dict(self._components["backend"])
        plan = resolve_edge_launch(self.paths, backend)
        self._spawn(plan)
        ready_file = Path(str(plan.metadata["readyFilePath"]))
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._processes.get("edge")
                if process is None or process.poll() is not None:
                    self._stop_component("edge")
                    raise WorkspaceHostError("os_start_failed", "OS 在就绪前退出")
            if ready_file.is_file():
                with self._lock:
                    self._components["edge"]["phase"] = "ready"
                    self._components["edge"]["diagnostic"] = None
                    self._components["edge"]["capabilities"] = ["device-control"]
                    self._publish_locked("os.ready", {"generation": plan.generation})
                    return self._snapshot_locked()
            time.sleep(0.1)
        self._stop_component("edge")
        raise WorkspaceHostError("os_readiness_failed", "等待 OS 就绪超时")

    def _start_plc(self) -> dict[str, object]:
        with self._lock:
            if self._components["plc"]["phase"] == "ready":
                return self._snapshot_locked()
        plan = resolve_plc_launch(self.paths)
        self._spawn(plan)
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._processes.get("plc")
                if process is None or process.poll() is not None:
                    raise WorkspaceHostError("plc_start_failed", "PLC-Sim 在就绪前退出")
            try:
                with urlopen(str(plan.ready_url), timeout=1.0) as response:
                    response.read()
                if response.status == 200:
                    break
            except (OSError, URLError):
                time.sleep(0.2)
        else:
            self._stop_component("plc")
            raise WorkspaceHostError(
                "plc_readiness_failed", "等待 PLC-Sim API 就绪超时"
            )
        metadata = plan.metadata
        self._post_json(
            f"{metadata['guiUrl']}/api/server/start",
            {
                "csv": metadata["variableTablePath"],
                "host": "127.0.0.1",
                "port": metadata["opcUaPort"],
            },
        )
        self._post_json(
            f"{metadata['guiUrl']}/api/agent/start",
            {
                "profile": metadata["handshakeProfile"],
                "workflow": metadata["handshakeWorkflow"],
                "host": "127.0.0.1",
                "port": metadata["opcUaPort"],
                "csv": metadata["variableTablePath"],
            },
        )
        with self._lock:
            component = self._components["plc"]
            component["phase"] = "ready"
            component["diagnostic"] = None
            component["capabilities"] = ["opcua-simulation", "handshake"]
            self._publish_locked("plc.ready", {"generation": plan.generation})
            return self._snapshot_locked()

    def _post_json(self, url: str, payload: dict[str, object]) -> None:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.readiness_timeout) as response:
                detail = response.read().decode("utf-8", errors="replace")
        except (OSError, URLError) as error:
            raise WorkspaceHostError("plc_configuration_failed", str(error)) from error
        if not 200 <= response.status < 300:
            raise WorkspaceHostError("plc_configuration_failed", detail)

    def _spawn(self, plan: LaunchPlan) -> None:
        with self._lock:
            component = self._components[plan.component]
            component.update(
                {
                    "phase": "starting",
                    "pid": None,
                    "address": plan.address,
                    "generation": plan.generation,
                    "logPath": str(plan.log_path),
                    "diagnostic": None,
                    "capabilities": [],
                    "metadata": dict(plan.metadata),
                }
            )
            self._publish_locked(
                f"{plan.component}.starting",
                {"generation": plan.generation},
            )
        plan.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with ProcessOutput(plan.log_path, LogPolicy.from_env(plan.environment)) as output:
                process = subprocess.Popen(
                    list(plan.command),
                    cwd=plan.cwd,
                    env=output.environment(plan.environment),
                    stdin=subprocess.DEVNULL,
                    stdout=output.stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
        except BaseException as error:
            with self._lock:
                component.update(
                    {
                        "phase": "failed",
                        "diagnostic": str(error),
                    }
                )
                self._publish_locked(
                    f"{plan.component}.failed", {"error": str(error)}
                )
            raise WorkspaceHostError(
                f"{plan.component}_start_failed",
                f"{plan.component} 启动失败：{error}",
            ) from error
        with self._lock:
            self._processes[plan.component] = process
            component["pid"] = process.pid
            self._publish_locked(
                f"{plan.component}.spawned",
                {"pid": process.pid, "generation": plan.generation},
            )

    def _wait_backend_ready(self, plan: LaunchPlan) -> dict[str, object]:
        """等待工站调度、库存与工作流接口共同就绪。

        参数：``plan`` 是本轮后端启动计划。返回领域包挂载投影。异常：地址缺失、
        超时或任一权威探针失败时抛 ``WorkspaceHostError``。
        """

        if not plan.address:
            raise WorkspaceHostError("backend_start_failed", "Backend 地址缺失")
        deadline = time.monotonic() + self.readiness_timeout
        readiness_payload = self._wait_backend_payload(
            plan,
            "/api/v1/readiness",
            _readiness_ready,
            deadline=deadline,
            allow_not_found=True,
        )
        # 旧 Backend 没有独立 readiness 端点时，退回原有的完整探针组合；只在
        # 明确 404 时兼容，不能把新 Backend 的 starting/failed 误当成 ready。
        probes = (
            [("/api/v1/health", _health_ready)]
            if readiness_payload is None
            else []
        )
        probes.extend(
            [
                ("/api/v1/devices", _successful_envelope),
                ("/api/v1/workflow-node-templates", _successful_envelope),
                (
                    "/api/v1/workflow-node-templates"
                    "?page=1&page_size=100&node_type=material_source",
                    _material_source_catalog_ready,
                ),
            ]
        )
        for path, accepts in probes:
            self._wait_backend_payload(plan, path, accepts, deadline=deadline)
        payload = self._wait_backend_payload(
            plan,
            "/api/v1/workspace/package-mounts",
            _package_mounts_ready,
            deadline=deadline,
        )
        assert payload is not None
        data = payload.get("data")
        assert isinstance(data, dict)
        return data

    def _wait_backend_payload(
        self,
        plan: LaunchPlan,
        path: str,
        accepts: Callable[[dict[str, object]], bool],
        *,
        deadline: float,
        allow_not_found: bool = False,
    ) -> dict[str, object] | None:
        while time.monotonic() < deadline:
            with self._lock:
                process = self._processes.get(plan.component)
                if process is None or process.poll() is not None:
                    raise WorkspaceHostError(
                        "backend_start_failed",
                        f"Backend 在 {path} 就绪前退出",
                    )
            payload: object = None
            status: int | None = None
            try:
                with urlopen(f"{plan.address}{path}", timeout=1.0) as response:
                    payload = json.loads(response.read())
                    status = response.status
            except HTTPError as response_error:
                status = response_error.code
                try:
                    payload = json.loads(response_error.read())
                except (OSError, ValueError):
                    payload = None
            except (OSError, ValueError, URLError):
                payload = None
            # 桌面固定端口可能被同时启动的另一工作区抢占，身份匹配前不能接管其调度器。
            if (path == "/api/v1/readiness" and status is not None
                    and plan.environment.get("UNILABOS_DESKTOP_WEB_HOST")
                    and (not isinstance(payload, dict) or payload.get("generation") != plan.generation)):
                self._stop_component(plan.component)
                raise WorkspaceHostError("backend_port_occupied", f"Backend 地址已由其他服务使用：{plan.address}")
            if allow_not_found and status == HTTPStatus.NOT_FOUND:
                return None
            if isinstance(payload, dict):
                if path == "/api/v1/readiness":
                    self._record_backend_readiness_progress(plan, payload)
                    if payload.get("status") == "failed":
                        self._stop_component(plan.component)
                        raise WorkspaceHostError(
                            "backend_readiness_failed",
                            "Backend 工作流运行时初始化失败：工作流源码或设备动作目录未能完成加载；"
                            "请先查看 backend.log 中最早的一条错误，再重试",
                        )
                if status == 200 and accepts(payload):
                    return payload
            time.sleep(0.2)
        self._stop_component(plan.component)
        raise WorkspaceHostError(
            "backend_readiness_failed",
            f"Backend 在规定时间内未就绪（等待接口：{path}）；请检查 backend.log，"
            "确认工作流运行时和设备动作目录已完成加载",
        )

    def _record_backend_readiness_progress(
        self,
        plan: LaunchPlan,
        payload: dict[str, object],
    ) -> None:
        """把 Backend 启动期 readiness 进度投影进 Host 权威快照。"""

        progress = payload.get("workflowProgress")
        if not isinstance(progress, dict):
            return
        loaded = progress.get("loaded")
        total = progress.get("total")
        if (
            not isinstance(loaded, int)
            or isinstance(loaded, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or loaded < 0
            or total < 0
            or loaded > total
        ):
            return
        normalized = {"loaded": loaded, "total": total}
        runtime_phase = payload.get("phase")
        with self._lock:
            component = self._components.get(plan.component)
            if (
                component is None
                or component.get("generation") != plan.generation
            ):
                return
            metadata = component.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                return
            if (
                metadata.get("workflowProgress") == normalized
                and metadata.get("workflowRuntimePhase") == runtime_phase
            ):
                return
            metadata["workflowProgress"] = normalized
            if isinstance(runtime_phase, str):
                metadata["workflowRuntimePhase"] = runtime_phase
            self._publish_locked(
                "backend.progress",
                {**normalized, "phase": runtime_phase},
            )

    def _stop_component(self, name: str) -> dict[str, object]:
        with self._lock:
            component = self._components[name]
            pid = component.get("pid")
            process = self._processes.pop(name, None)
            if not isinstance(pid, int) or pid < 1:
                component.update(idle_component(name))
                self._publish_locked(f"{name}.idle", {})
                return self._snapshot_locked()
            component["phase"] = "stopping"
            self._publish_locked(f"{name}.stopping", {"pid": pid})
        _terminate_process_tree(pid, process)
        with self._lock:
            previous = dict(component)
            component.update(idle_component(name))
            component["generation"] = previous.get("generation")
            component["logPath"] = previous.get("logPath")
            component["metadata"] = previous.get("metadata", {})
            self._publish_locked(f"{name}.idle", {"pid": pid})
            return self._snapshot_locked()

    def _update_configuration(self, parameters: dict[str, object]) -> dict[str, object]:
        """校验并持久化工作区配置更新，返回包含新配置的 Host 快照。

        Args:
            parameters: 前端请求更新的配置字段和值。

        Returns:
            配置写入后发布的完整 Workspace Host 快照。

        Raises:
            WorkspaceHostError: 配置包含未知字段或外部设备包范围不是布尔值。
        """

        allowed = {
            "graphPath",
            "externalDevicesOnly",
            "runtimeMode",
            "plcSimulatorProjectPath",
            "plcVariableTablePath",
            "plcHandshakeProfile",
            "plcHandshakeWorkflow",
            "domainMode",
            "schedulerUrl",
        }
        unknown = sorted(set(parameters) - allowed)
        if unknown:
            raise WorkspaceHostError(
                "configuration_invalid",
                f"未知配置字段：{', '.join(unknown)}",
            )
        if (
            "externalDevicesOnly" in parameters
            and not isinstance(parameters["externalDevicesOnly"], bool)
        ):
            raise WorkspaceHostError(
                "configuration_invalid",
                "externalDevicesOnly 必须是布尔值",
            )
        if parameters.get("domainMode", "local") != "local":
            raise WorkspaceHostError(
                "backend_mode_removed",
                "当前 OS 只支持 local 控制面，不再支持 backend 模式",
            )
        normalized = dict(parameters)
        if "schedulerUrl" in normalized:
            value = normalized["schedulerUrl"]
            if value is not None and not isinstance(value, str):
                raise WorkspaceHostError(
                    "scheduler_url_invalid",
                    "Scheduler 地址必须是 HTTP(S) 服务地址",
                )
            normalized["schedulerUrl"] = self._normalize_scheduler_url(value)
        with self._lock:
            self._configuration.update(normalized)
            if "schedulerUrl" in normalized:
                backend_metadata = self._components["backend"].get("metadata")
                if isinstance(backend_metadata, dict):
                    backend_metadata["schedulerUrl"] = normalized["schedulerUrl"]
            payload = {"schemaVersion": 1, **self._configuration}
            atomic_write_json(self.paths.environment, payload)
            self._publish_locked("configuration.updated", normalized)
            return self._snapshot_locked()

    def _switch_authority(
        self,
        parameters: dict[str, object],
        *,
        bootstrap: bool = True,
    ) -> dict[str, object]:
        """拒绝已移除的远端控制面切换请求。

        参数：``parameters`` 是历史 Authority 切换参数，``bootstrap`` 保留用于
        旧调用方的签名兼容。返回：无；该功能已关闭。异常：始终抛出
        ``WorkspaceHostError``，确保不会连接或写入 Backend。
        """

        raise WorkspaceHostError(
            "backend_mode_removed",
            "当前 OS 固定使用 local 控制面，Authority 切换功能已移除",
        )

    def _publish_release(self, parameters: dict[str, object]) -> dict[str, object]:
        """Publish the visible Local generation and optionally activate Backend Authority."""

        unknown = sorted(set(parameters) - {
            "backendUrl", "schedulerUrl", "activate", "verify", "resetTarget",
            "confirmation"
        })
        if unknown:
            raise WorkspaceHostError(
                "release_parameters_invalid",
                f"未知发布字段：{', '.join(unknown)}",
            )
        backend_url = self._normalize_backend_url(
            _optional_text(parameters.get("backendUrl"))
        )
        with self._lock:
            configured_scheduler_url = self._configuration.get("schedulerUrl")
        scheduler_url = self._normalize_scheduler_url(
            parameters.get("schedulerUrl", configured_scheduler_url)
        )
        activate = bool(parameters.get("activate", False))
        reset_target = parameters.get("resetTarget", False) is True
        if reset_target and parameters.get("confirmation") != "CLEAR_BACKEND":
            raise WorkspaceHostError(
                "release_target_reset_confirmation_required",
                "清空 Backend 必须提供明确确认",
            )
        if parameters.get("verify", True) is not True:
            raise WorkspaceHostError(
                "release_verification_required", "WorkspaceRelease 不允许跳过回读校验"
            )
        with self._lock:
            domain_mode = str(self._configuration.get("domainMode") or "local")
            backend_ready = self._components["backend"]["phase"] == "ready"
            edge_ready = self._components["edge"]["phase"] == "ready"
        if domain_mode != "local":
            raise WorkspaceHostError(
                "release_source_not_local", "WorkspaceRelease 只能从 Local Authority 构建"
            )
        if scheduler_url:
            self._preflight_backend_authority(backend_url, scheduler_url)
        else:
            self._preflight_backend_authority(backend_url)
        temporary_backend = not backend_ready
        if temporary_backend:
            self._start_backend({})
        try:
            with self._lock:
                component = dict(self._components["backend"])
            source_address = _optional_text(component.get("address"))
            if not source_address:
                raise WorkspaceHostError(
                    "release_source_unavailable", "Local Backend 尚未就绪"
                )
            from .release_publish import create_existing_backend_publisher

            staged_authority: dict[str, object] | None = None
            managed_edge_staged_before_reset = False

            def stage_device_authority() -> None:
                nonlocal staged_authority, managed_edge_staged_before_reset
                if staged_authority is not None:
                    # ``resetTarget`` deletes and recreates every Backend
                    # Material after the activation preflight.  A managed Edge
                    # that stays connected keeps capabilities bound to the
                    # deleted device UUIDs, so Backend rejects the recreated
                    # workflow nodes as undeclared actions.  Reconnect after
                    # material import and before workflow import to resolve the
                    # new stable-barcode identities and register capabilities
                    # against the current devices.
                    if managed_edge_staged_before_reset:
                        self._stop_component("edge")
                        self._start_edge()
                        managed_edge_staged_before_reset = False
                    return
                staged_authority = self._switch_authority(
                    {
                        "mode": "backend",
                        "backendUrl": backend_url,
                        "schedulerUrl": scheduler_url,
                    },
                    bootstrap=False,
                )
                # ``_switch_authority`` already reconnects an Edge that was
                # running before the transition.  Do not manufacture a second
                # local Edge when this Workspace had none: Backend deployments
                # commonly own their Edge in Kubernetes, and duplicate
                # registration must remain an error.  Workflow import will
                # validate against whichever Edge capabilities are already
                # active on the target authority.

            publisher = create_existing_backend_publisher(
                source_address=source_address,
                source_workspace=self.paths.workspace,
                target_address=backend_url,
                credential=os.environ.get("UNILAB_BACKEND_API_KEY") or self.token,
                deployment_directory=self.paths.root / "deployments",
                timeout=self.readiness_timeout,
                before_workflows=stage_device_authority if activate else None,
            )
            prepared_release = None
            with self._lock:
                self._publish_locked(
                    "release.publish.started", {"backendUrl": backend_url}
                )
            try:
                if reset_target:
                    # Freeze every Local fact and prove the managed Backend/Edge
                    # can start before deleting target data.  A startup failure
                    # must leave the existing Backend untouched.
                    prepared_release = publisher.build()
                    if activate:
                        stage_device_authority()
                        managed_edge_staged_before_reset = edge_ready
                    from .release_publish import ExistingBackendDeploymentTarget

                    ExistingBackendDeploymentTarget(
                        backend_url,
                        os.environ.get("UNILAB_BACKEND_API_KEY") or self.token,
                        timeout=self.readiness_timeout,
                    ).clear()
                receipt = publisher.publish(prepared_release)
            except BaseException:
                if staged_authority is not None:
                    if not edge_ready:
                        self._stop_component("edge")
                    self._switch_authority({"mode": "local"})
                raise
            with self._lock:
                self._publish_locked("release.publish.succeeded", receipt)
        finally:
            if temporary_backend:
                self._stop_component("backend")
        if activate:
            snapshot = staged_authority or self._switch_authority(
                {"mode": "backend", "backendUrl": backend_url}
            )
            receipt = {**receipt, "activated": True, "authority": snapshot}
        else:
            receipt = {**receipt, "activated": False}
        return receipt

    def _inspect_release_target(
        self, parameters: dict[str, object]
    ) -> dict[str, object]:
        """Inspect a publish target without changing its data."""

        unknown = sorted(set(parameters) - {"backendUrl"})
        if unknown:
            raise WorkspaceHostError(
                "release_parameters_invalid",
                f"未知检查字段：{', '.join(unknown)}",
            )
        backend_url = self._normalize_backend_url(
            _optional_text(parameters.get("backendUrl"))
        )
        self._preflight_backend_authority(backend_url)
        from .release_publish import ExistingBackendDeploymentTarget

        return ExistingBackendDeploymentTarget(
            backend_url,
            os.environ.get("UNILAB_BACKEND_API_KEY") or self.token,
            timeout=self.readiness_timeout,
        ).inspect()

    def _bootstrap_backend_authority(self, backend_url: str) -> None:
        """用当前 Local Backend 投影初始化并收敛目标 Backend Authority。

        Args:
            backend_url: 目标 Backend 的规范服务地址。

        Returns:
            无返回值；成功计数通过 Workspace Host 事件发布。

        Raises:
            WorkspaceHostError: Local Backend、设备图、凭据或目标同步失败时抛出。
        """

        from .authority_sync import BackendAuthorityBootstrapper

        with self._lock:
            component = dict(self._components["backend"])
        source_address = _optional_text(component.get("address"))
        metadata = component.get("metadata")
        graph_path = (
            _optional_text(metadata.get("graphPath"))
            if isinstance(metadata, dict)
            else None
        )
        if not source_address or not graph_path:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Local Backend 缺少模板投影地址或当前设备图",
            )
        credential = os.environ.get("UNILAB_BACKEND_API_KEY") or self.token
        with self._lock:
            self._publish_locked(
                "authority.bootstrap.started", {"backendUrl": backend_url}
            )
        report = BackendAuthorityBootstrapper(
            source_address,
            backend_url,
            credential,
            timeout=self.readiness_timeout,
        ).bootstrap(Path(graph_path))
        with self._lock:
            self._publish_locked(
                "authority.bootstrap.succeeded",
                {
                    "backendUrl": backend_url,
                    "templateCount": report.template_count,
                    "createdMaterialCount": report.created_material_count,
                    "existingMaterialCount": report.existing_material_count,
                },
            )

    def _replace_configuration(
        self, configuration: dict[str, object], event: str
    ) -> None:
        with self._lock:
            self._configuration = dict(configuration)
            atomic_write_json(
                self.paths.environment,
                {"schemaVersion": 1, **self._configuration},
            )
            self._publish_locked(event, {"domainMode": configuration.get("domainMode")})

    @staticmethod
    def _normalize_backend_url(value: str | None) -> str:
        if value is None:
            raise WorkspaceHostError(
                "backend_url_missing", "切换到 Backend Authority 必须提供 backendUrl"
            )
        normalized = value.rstrip("/")
        if normalized.endswith("/api/v1"):
            normalized = normalized.removesuffix("/api/v1").rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WorkspaceHostError(
                "backend_url_invalid", "backendUrl 必须是 HTTP(S) 服务地址"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WorkspaceHostError(
                "backend_url_invalid", "backendUrl 不能包含凭据、查询参数或片段"
            )
        return normalized

    @staticmethod
    def _normalize_scheduler_url(value: object) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise WorkspaceHostError(
                "scheduler_url_invalid",
                "Scheduler 地址必须是 HTTP(S) 服务地址",
            )
        try:
            return normalize_scheduler_address(value)
        except ValueError as error:
            raise WorkspaceHostError("scheduler_url_invalid", str(error)) from error

    def _preflight_backend_authority(
        self,
        backend_url: str,
        scheduler_url: str | None = None,
    ) -> None:
        for path in ("/api/v1/health", "/api/v1/workflows?page=1&page_size=1"):
            try:
                with urlopen(f"{backend_url}{path}", timeout=3.0) as response:
                    response.read()
                if not 200 <= response.status < 300:
                    raise WorkspaceHostError(
                        "backend_authority_unavailable",
                        f"Backend Authority 预检失败：{path} HTTP {response.status}",
                    )
            except WorkspaceHostError:
                raise
            except (OSError, URLError) as error:
                raise WorkspaceHostError(
                    "backend_authority_unavailable",
                    f"Backend Authority 预检失败：{path}：{error}",
                ) from error

        # Edge Runtime registers itself through the Scheduler Authority, not
        # the Backend API origin. Probe route existence before stopping the
        # currently healthy local OS. A GET commonly returns 405 for a
        # POST-only route, which still proves the required contract exists;
        # 404 means this Scheduler cannot be used as an Authority yet.
        edge_path = "/api/v1/edge/sessions"
        try:
            scheduler_url = resolve_scheduler_address(backend_url, scheduler_url)
        except ValueError as error:
            raise WorkspaceHostError("scheduler_url_invalid", str(error)) from error
        try:
            with urlopen(f"{scheduler_url}{edge_path}", timeout=3.0) as response:
                response.read()
            status = response.status
        except HTTPError as error:
            status = error.code
        except (OSError, URLError) as error:
            raise WorkspaceHostError(
                "backend_authority_unavailable",
                f"Backend Authority Edge 控制接口预检失败：{edge_path}：{error}",
            ) from error
        if status == HTTPStatus.NOT_FOUND:
            raise WorkspaceHostError(
                "backend_authority_incompatible",
                "目标 Scheduler 暂不支持 Edge 调度连接，"
                "请启动包含 Edge 控制接口的 Scheduler 后重试",
            )
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise WorkspaceHostError(
                "backend_authority_unavailable",
                "Backend 的 Scheduler 未就绪，Edge 暂时无法连接。"
                "请启动 Scheduler 服务（默认端口 8081）后重试；"
                "当前 OS 尚未切换。",
            )

    def _attach_renderer(self, parameters: dict[str, object]) -> dict[str, object]:
        pid = parameters.get("pid")
        address = parameters.get("address")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise WorkspaceHostError("renderer_invalid", "Renderer PID 无效")
        if not isinstance(address, str) or not address.startswith("http://127.0.0.1:"):
            raise WorkspaceHostError(
                "renderer_invalid", "Renderer 必须使用 loopback 地址"
            )
        if not _pid_exists(pid):
            raise WorkspaceHostError("renderer_invalid", f"Renderer 进程不存在：{pid}")
        with self._lock:
            generation = str(parameters.get("generation") or pid)
            previous_metadata = self._components["renderer"].get("metadata")
            previous_metadata = (
                dict(previous_metadata) if isinstance(previous_metadata, dict) else {}
            )
            workbench_project = _optional_text(parameters.get("workbenchProjectPath"))
            node_executable = _optional_text(parameters.get("nodeExecutable"))
            if workbench_project:
                self._configuration["workbenchProjectPath"] = workbench_project
            if node_executable:
                self._configuration["workbenchNodeExecutable"] = node_executable
            if workbench_project or node_executable:
                atomic_write_json(
                    self.paths.environment,
                    {"schemaVersion": 1, **self._configuration},
                )
            self._components["renderer"].update(
                {
                    "phase": "ready",
                    "pid": pid,
                    "address": address,
                    "generation": generation,
                    "logPath": None,
                    "diagnostic": None,
                    "capabilities": [
                        "workbench-ui",
                        "theia-rpc",
                        "material-scene-inspect",
                        "material-scene-capture",
                        "material-scene-reload",
                    ],
                    "metadata": {
                        **previous_metadata,
                        "automationBaseUrl": (
                            f"{address.rstrip('/')}/__unilab_renderer/v1"
                        ),
                        "automationContract": "unilab-material-renderer/v1",
                        **(
                            {"workbenchProjectPath": workbench_project}
                            if workbench_project
                            else {}
                        ),
                        **(
                            {"nodeExecutable": node_executable}
                            if node_executable
                            else {}
                        ),
                    },
                }
            )
            self._publish_locked("renderer.attached", {"pid": pid, "address": address})
            return self._snapshot_locked()

    def _ensure_headless_renderer(self) -> dict[str, object]:
        """Launch the normal Workbench + Chromium adapter when no UI is attached."""

        with self._lock:
            renderer = dict(self._components["renderer"])
        if renderer.get("phase") == "ready" and self._material_renderer_usable(
            renderer
        ):
            return {"status": "ready", "adapter": "attached", "renderer": renderer}
        metadata = renderer.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        with self._lock:
            configuration = dict(self._configuration)
        project = _optional_text(metadata.get("workbenchProjectPath")) or _optional_text(
            configuration.get("workbenchProjectPath")
        )
        node_executable = _optional_text(metadata.get("nodeExecutable")) or _optional_text(
            configuration.get("workbenchNodeExecutable")
        )
        if not project or not node_executable:
            raise WorkspaceHostError(
                "headless_renderer_not_configured",
                "尚未发现已安装 Workbench renderer；请先启动一次 UniLab Workbench",
            )
        project_path = Path(project).resolve()
        script = project_path / "scripts" / "headless-renderer.mjs"
        backend = project_path / "lib" / "backend" / "main.js"
        if not script.is_file() or not backend.is_file() or not Path(node_executable).is_file():
            raise WorkspaceHostError(
                "headless_renderer_not_configured",
                f"Workbench headless renderer 未构建：{project_path}",
            )
        generation = str(uuid.uuid4())
        runtime = self.paths.runtime / "renderer" / generation
        runtime.mkdir(parents=True, exist_ok=False)
        ready_file = runtime / "ready.json"
        port = available_loopback_port()
        log_path = self.paths.logs / f"{generation}-renderer.log"
        command = [
            node_executable,
            str(script),
            "--workspace",
            str(self.paths.workspace),
            "--port",
            str(port),
            "--ready-file",
            str(ready_file),
        ]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = _renderer_process_environment(configuration)
        with ProcessOutput(log_path, LogPolicy.from_env(environment)) as output:
            process = subprocess.Popen(
                command,
                cwd=project_path,
                env=output.environment(environment),
                stdin=subprocess.DEVNULL,
                stdout=output.stream,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        deadline = time.monotonic() + max(self.readiness_timeout, 120.0)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise WorkspaceHostError(
                    "headless_renderer_start_failed",
                    f"Headless renderer 在就绪前退出；日志：{log_path}",
                )
            if ready_file.is_file():
                try:
                    ready = read_json(ready_file)
                except WorkspaceHostError:
                    time.sleep(0.25)
                    continue
                address = _optional_text(ready.get("address"))
                if not address or not address.startswith("http://127.0.0.1:"):
                    _terminate_process_tree(process.pid, process)
                    raise WorkspaceHostError(
                        "headless_renderer_start_failed",
                        "Headless renderer 返回了无效 loopback 地址",
                    )
                current = idle_component("renderer")
                current.update(
                    {
                        "phase": "ready",
                        "pid": process.pid,
                        "address": address,
                        "generation": generation,
                        "logPath": str(log_path),
                        "diagnostic": None,
                        "capabilities": [
                            "workbench-ui",
                            "theia-rpc",
                            "material-scene-inspect",
                            "material-scene-capture",
                            "material-scene-reload",
                        ],
                        "metadata": {
                            "automationBaseUrl": (
                                f"{address.rstrip('/')}/__unilab_renderer/v1"
                            ),
                            "automationContract": "unilab-material-renderer/v1",
                            "workbenchProjectPath": str(project_path),
                            "nodeExecutable": node_executable,
                            "adapter": "headless",
                            "headlessLauncherPid": process.pid,
                            "headlessLogPath": str(log_path),
                        },
                    }
                )
                with self._lock:
                    self._components["renderer"] = current
                    self._publish_locked(
                        "renderer.headless.ready",
                        {"generation": generation, "launcherPid": process.pid},
                    )
                return {
                    "status": "ready",
                    "adapter": "headless",
                    "generation": generation,
                    "launcherPid": process.pid,
                    "renderer": current,
                }
            time.sleep(0.25)
        _terminate_process_tree(process.pid, process)
        raise WorkspaceHostError(
            "headless_renderer_readiness_failed",
            f"等待 Headless renderer 就绪超时；日志：{log_path}",
        )

    def _material_renderer_usable(self, renderer: dict[str, object]) -> bool:
        metadata = renderer.get("metadata")
        base_url = (
            _optional_text(metadata.get("automationBaseUrl"))
            if isinstance(metadata, dict)
            else None
        )
        if not base_url:
            return False
        try:
            self._json_request(
                f"{base_url.rstrip('/')}/material/scene?view=2.5d",
                authorization=True,
            )
        except WorkspaceHostError:
            return False
        return True

    def _stop_headless_renderer(self) -> dict[str, object]:
        with self._lock:
            renderer = dict(self._components["renderer"])
        metadata = renderer.get("metadata")
        launcher_pid = (
            metadata.get("headlessLauncherPid")
            if isinstance(metadata, dict)
            else None
        )
        if not isinstance(launcher_pid, int) or launcher_pid < 1:
            return {"status": "not-running"}
        _terminate_process_tree(launcher_pid, None)
        with self._lock:
            preserved = dict(metadata) if isinstance(metadata, dict) else {}
            preserved.pop("headlessLauncherPid", None)
            self._components["renderer"].update(idle_component("renderer"))
            self._components["renderer"]["metadata"] = preserved
            self._publish_locked("renderer.headless.stopped", {"pid": launcher_pid})
        return {"status": "stopped", "launcherPid": launcher_pid}

    def _detach_renderer(self, parameters: dict[str, object]) -> dict[str, object]:
        requested_pid = parameters.get("pid")
        with self._lock:
            current_pid = self._components["renderer"].get("pid")
            if requested_pid is not None and requested_pid != current_pid:
                return self._snapshot_locked()
            self._components["renderer"].update(idle_component("renderer"))
            self._publish_locked("renderer.detached", {"pid": current_pid})
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, object]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "revision": self._revision,
            "eventCursor": self._cursor,
            "workspacePath": str(self.paths.workspace),
            "host": {
                "phase": "ready",
                "pid": os.getpid(),
                "endpoint": self._endpoint,
                "tokenPath": str(self.paths.token),
                "platform": sys_platform(),
                "capabilities": [DATABASE_SELECTION_CAPABILITY, STATION_FILE_CAPABILITY],
            },
            "configuration": dict(self._configuration),
            "components": {
                name: dict(component) for name, component in self._components.items()
            },
            "updatedAt": utc_timestamp(),
        }

    def _publish_locked(self, event: str, details: object) -> None:
        self._revision += 1
        self._cursor += 1
        snapshot = self._snapshot_locked()
        atomic_write_json(self.paths.session, snapshot)
        self._audit_locked(event, details)

    def _audit_locked(self, event: str, details: object) -> None:
        record = {
            "schemaVersion": SCHEMA_VERSION,
            "cursor": self._cursor,
            "timestamp": utc_timestamp(),
            "event": event,
            "details": _public_database_parameters(details),
        }
        self.paths.audit.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")

    def _monitor_processes(self) -> None:
        while not self._closed.wait(0.25):
            with self._lock:
                self._refresh_processes_locked()
                now = time.monotonic()
                recover = [
                    name
                    for name, due_at in self._recovery_pending.items()
                    if due_at <= now
                    and self._components[name].get("phase") == "failed"
                ]
                for name in recover:
                    self._recovery_pending.pop(name, None)
            for name in sorted(recover, key=_recovery_order):
                self._recover_component(name)

    def _recover_component(self, name: str) -> None:
        """Restart one unexpectedly exited managed component with bounded backoff."""

        with self._lifecycle_lock:
            with self._lock:
                if self._components[name].get("phase") != "failed":
                    self._recovery_attempts.pop(name, None)
                    return
                edge_was_ready = self._components["edge"].get("phase") == "ready"
                self._publish_locked(
                    f"{name}.recovery.started",
                    {"attempt": self._recovery_attempts.get(name, 0) + 1},
                )
            try:
                if name == "backend":
                    self._start_backend({})
                    if edge_was_ready:
                        self._stop_component("edge")
                        self._start_edge()
                elif name == "edge":
                    self._start_edge()
                elif name == "plc":
                    self._start_plc()
                else:
                    return
            except BaseException as error:  # noqa: BLE001 - supervision boundary.
                with self._lock:
                    attempts = self._recovery_attempts.get(name, 0) + 1
                    self._recovery_attempts[name] = attempts
                    delay = min(30.0, float(2 ** min(attempts - 1, 5)))
                    component = self._components[name]
                    component["phase"] = "failed"
                    component["diagnostic"] = f"自动恢复失败：{error}"
                    self._recovery_pending[name] = time.monotonic() + delay
                    self._publish_locked(
                        f"{name}.recovery.failed",
                        {"attempt": attempts, "retryAfterSeconds": delay},
                    )
                return
            with self._lock:
                self._recovery_attempts.pop(name, None)
                self._recovery_pending.pop(name, None)
                self._publish_locked(f"{name}.recovery.succeeded", {})

    def _refresh_processes_locked(self) -> None:
        for name, process in tuple(self._processes.items()):
            return_code = process.poll()
            if return_code is None:
                continue
            self._processes.pop(name, None)
            component = self._components[name]
            previous_phase = component.get("phase")
            if component["phase"] == "stopping":
                continue
            component["phase"] = "failed"
            component["diagnostic"] = f"进程意外退出，exit_code={return_code}"
            self._publish_locked(
                f"{name}.exited",
                {"pid": process.pid, "exitCode": return_code},
            )
            if name in _SUPERVISED_COMPONENTS and previous_phase == "ready":
                self._recovery_pending.setdefault(name, time.monotonic())
        for name, component in self._components.items():
            if name in self._processes:
                continue
            pid = component.get("pid")
            if (
                component.get("phase") not in {"ready", "interrupted"}
                or not isinstance(pid, int)
                or pid < 1
                or _pid_exists(pid)
            ):
                continue
            component["phase"] = "failed"
            component["diagnostic"] = "已接管的进程不再存在"
            self._publish_locked(f"{name}.exited", {"pid": pid, "adopted": True})
            if name in _SUPERVISED_COMPONENTS:
                self._recovery_pending.setdefault(name, time.monotonic())

    def _initial_configuration(self) -> dict[str, object]:
        """读取持久化配置并补齐本地模式默认值。

        参数：无。返回：不含 ``schemaVersion``、且控制面固定为 ``local`` 的配置
        字典。异常：持久化的外部设备包范围不是布尔值，或旧配置仍声明
        ``domainMode=backend`` 时抛出 ``WorkspaceHostError``；不会启动远端连接。
        状态不变量：Host 创建后所有组件都只能使用本工作区的本地 Scheduler。
        """

        try:
            payload = json.loads(self.paths.environment.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "graphPath": None,
                "externalDevicesOnly": True,
                "runtimeMode": "normal",
                "domainMode": "local",
                "backendUrl": None,
                "schedulerUrl": None,
            }
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            return {
                "graphPath": None,
                "externalDevicesOnly": True,
                "runtimeMode": "normal",
                "domainMode": "local",
                "backendUrl": None,
                "schedulerUrl": None,
            }
        configuration = {
            key: value for key, value in payload.items() if key != "schemaVersion"
        }
        had_legacy_startup_mode = "startupMode" in configuration
        # 旧版本把启动可见范围写入环境文件。该字段不再参与启动计划，读取时
        # 直接丢弃，避免新 Host 的 snapshot/session 继续传播已经删除的概念。
        configuration.pop("startupMode", None)
        configuration.setdefault("externalDevicesOnly", True)
        if not isinstance(configuration["externalDevicesOnly"], bool):
            raise WorkspaceHostError(
                "configuration_invalid",
                "externalDevicesOnly 必须是布尔值",
            )
        configuration.setdefault("domainMode", "local")
        if configuration["domainMode"] != "local":
            raise WorkspaceHostError(
                "backend_mode_removed",
                "当前 OS 只支持 local 控制面，请删除旧配置中的 backend 模式",
            )
        configuration.setdefault("backendUrl", None)
        configuration["schedulerUrl"] = self._normalize_scheduler_url(
            configuration.get("schedulerUrl")
        )
        if had_legacy_startup_mode:
            # 迁移磁盘上的旧环境文件，避免下一次启动再次读到已删除的字段。
            migrated_payload = dict(payload)
            migrated_payload.pop("startupMode", None)
            atomic_write_json(self.paths.environment, migrated_payload)
        return configuration

    def _restore_interrupted_components(self) -> None:
        try:
            previous = read_json(self.paths.session)
        except WorkspaceHostError:
            return
        if previous.get("schemaVersion") != SCHEMA_VERSION:
            return
        components = previous.get("components")
        if not isinstance(components, dict):
            return
        for name in COMPONENT_NAMES:
            value = components.get(name)
            if not isinstance(value, dict):
                continue
            pid = value.get("pid")
            if isinstance(pid, int) and pid > 0 and _pid_exists(pid):
                restored = dict(value)
                metadata = restored.get("metadata")
                if isinstance(metadata, dict):
                    metadata = dict(metadata)
                    metadata.pop("startupMode", None)
                    restored["metadata"] = metadata
                if self._component_is_ready(name, restored):
                    restored["phase"] = "ready"
                    restored["diagnostic"] = None
                else:
                    restored["phase"] = "interrupted"
                    restored["diagnostic"] = (
                        "Workspace Host 重启后发现遗留进程，但就绪性无法证明；"
                        "可通过同一控制面停止"
                    )
                self._components[name] = restored

    @staticmethod
    def _component_is_ready(name: str, component: dict[str, object]) -> bool:
        if name == "edge":
            metadata = component.get("metadata")
            ready_path = (
                metadata.get("readyFilePath") if isinstance(metadata, dict) else None
            )
            return isinstance(ready_path, str) and Path(ready_path).is_file()
        address = component.get("address")
        if not isinstance(address, str) or not address:
            return name == "renderer"
        if name != "backend":
            path = "/" if name == "renderer" else "/api/state"
            try:
                with urlopen(f"{address}{path}", timeout=0.5) as response:
                    response.read()
                return response.status == 200
            except (OSError, URLError):
                return False
        for path, accepts in (
            ("/api/v1/readiness", _readiness_ready),
            ("/api/v1/health", _health_ready),
        ):
            try:
                with urlopen(f"{address}{path}", timeout=0.5) as response:
                    payload = json.loads(response.read())
                return (
                    response.status == 200
                    and isinstance(payload, dict)
                    and accepts(payload)
                )
            except HTTPError as response_error:
                if (
                    path == "/api/v1/readiness"
                    and response_error.code == HTTPStatus.NOT_FOUND
                ):
                    continue
                return False
            except (OSError, ValueError, URLError):
                return False
        return False


def _handler_type(host: WorkspaceHost) -> type[BaseHTTPRequestHandler]:
    class WorkspaceHostHandler(BaseHTTPRequestHandler):
        server_version = "UniLabWorkspaceHost/1"

        def do_GET(self) -> None:
            if not self._authorize():
                return
            try:
                if self.path == "/v1/snapshot":
                    self._json(HTTPStatus.OK, host.snapshot())
                    return
                if self.path.startswith("/v1/operations/"):
                    operation_id = self.path.removeprefix("/v1/operations/")
                    self._json(HTTPStatus.OK, host.operation(operation_id))
                    return
                if self.path.startswith("/v1/logs/"):
                    component, _, query = self.path.removeprefix("/v1/logs/").partition(
                        "?"
                    )
                    max_bytes = 64 * 1024
                    if query.startswith("maxBytes="):
                        max_bytes = int(query.removeprefix("maxBytes="))
                    self._json(HTTPStatus.OK, host.log_tail(component, max_bytes))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
            except WorkspaceHostError as error:
                self._json(_error_status(error), {"error": error.as_dict()})
            except (TypeError, ValueError):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "invalid_request", "message": "请求参数无效"}},
                )

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if self.path != "/v1/operations":
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            try:
                operation = host.submit(self._body())
                self._json(HTTPStatus.ACCEPTED, operation)
            except WorkspaceHostError as error:
                self._json(_error_status(error), {"error": error.as_dict()})

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _authorize(self) -> bool:
            if host.authorized(self.headers.get("Authorization")):
                return True
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"code": "unauthorized", "message": "token 无效"}},
            )
            return False

        def _body(self) -> object:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise WorkspaceHostError(
                    "invalid_request", "Content-Length 无效"
                ) from error
            if length < 1 or length > _MAX_BODY_BYTES:
                raise WorkspaceHostError("invalid_request", "请求体大小无效")
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise WorkspaceHostError(
                    "invalid_request", "请求体不是有效 JSON"
                ) from error

        def _json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return WorkspaceHostHandler


def _error_status(error: WorkspaceHostError) -> HTTPStatus:
    if error.code in {"operation_not_found", "host_not_found"}:
        return HTTPStatus.NOT_FOUND
    if error.code in {
        "operation_conflict",
        "revision_conflict",
        "host_already_running",
    }:
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceHostError("invalid_request", f"{field} 必须是非空字符串")
    if field == "operationId" and any(character in value for character in "/\\"):
        raise WorkspaceHostError("invalid_request", "operationId 无效")
    return value


def _readiness_ready(payload: dict[str, object]) -> bool:
    return payload.get("status") == "ready"


def _health_ready(payload: dict[str, object]) -> bool:
    return payload.get("status") == "ok"


def _successful_envelope(payload: dict[str, object]) -> bool:
    return payload.get("code") == 0


def _catalog_items(payload: dict[str, object]) -> list[object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    return items if isinstance(items, list) else []


def _material_source_catalog_ready(payload: dict[str, object]) -> bool:
    return payload.get("code") == 0 and any(
        isinstance(item, dict)
        and item.get("node_type") == "material_source"
        and isinstance(item.get("uuid"), str)
        for item in _catalog_items(payload)
    )


def _package_mounts_ready(payload: dict[str, object]) -> bool:
    data = payload.get("data")
    return (
        payload.get("code") == 0
        and isinstance(data, dict)
        and data.get("schemaVersion") == "workspace-package-mounts/v1"
        and isinstance(data.get("items"), list)
        and bool(data["items"])
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _renderer_process_environment(
    configuration: dict[str, object],
    *,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the renderer environment from the selected workspace authority."""

    environment = dict(os.environ if base_environment is None else base_environment)
    environment["UNILAB_RENDERER_MANAGED_HEADLESS"] = "1"
    backend_url = _optional_text(configuration.get("backendUrl"))
    if configuration.get("domainMode") == "backend" and backend_url:
        environment["UNILAB_BACKEND_PROXY_TARGET"] = backend_url
    return environment


def _pid_exists(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False
    if os.name == "nt":
        # CPython implements os.kill() on Windows with TerminateProcess for
        # ordinary signal values. Consequently os.kill(pid, 0) TERMINATES a
        # live process with exit code 0 instead of probing for its existence.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if sys.platform.startswith("linux"):
        try:
            # comm 可以包含空格及右括号，状态字段在最后一个右括号之后。
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            state = stat.rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except (OSError, UnicodeError, IndexError):
            # procfs 不可用时保留原探测路径，不能把读取失败当成进程退出。
            pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1,
        )
        if completed.returncode == 0 and completed.stdout.lstrip().startswith(b"Z"):
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass
    return True


def _recovery_order(name: str) -> int:
    """Recover authorities before their device and simulator dependants."""

    return {"backend": 0, "plc": 1, "edge": 2}.get(name, 99)


def _terminate_process_tree(pid: int, process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is not None:
        return
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise WorkspaceHostError("stop_failed", "Windows 缺少 SystemRoot")
        completed = subprocess.run(
            [
                str(Path(system_root) / "System32" / "taskkill.exe"),
                "/PID",
                str(pid),
                "/T",
                "/F",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_STOP_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0 and _pid_exists(pid):
            raise WorkspaceHostError("stop_failed", f"无法停止进程树：{pid}")
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if not _pid_exists(pid):
            return
        raise
        return
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process is not None:
            if process.poll() is not None:
                return
        elif not _pid_exists(pid):
            return
        time.sleep(0.05)
    if (process is not None and process.poll() is None) or (
        process is None and _pid_exists(pid)
    ):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if not _pid_exists(pid):
                return
            raise
            return
    if process is not None:
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise WorkspaceHostError("stop_failed", f"进程树未退出：{pid}") from error


def sys_platform() -> str:
    import platform

    return platform.system().lower()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 Workspace Host 服务进程的命令行参数。

    参数：``argv`` 是可选参数序列；为 ``None`` 时读取当前进程参数。返回：包含
    工作区、监听地址、固定 Backend 端口和就绪预算的 ``argparse.Namespace``。异常：
    参数缺失、格式错误或取值越界时 ``argparse`` 抛出 ``SystemExit``。
    """

    parser = argparse.ArgumentParser(description="Start one Uni-Lab Workspace Host")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--backend-port", default=None, type=int)
    parser.add_argument(
        "--readiness-timeout", default=_READINESS_TIMEOUT_SECONDS, type=float
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """启动并托管单实例 Workspace Host HTTP 服务直到收到停止信号。

    参数：``argv`` 是可选命令行参数序列。返回：无；服务退出前关闭 HTTP Server、
    Host 线程并释放单实例锁。异常：参数无效时抛 ``SystemExit``；工作区准备、
    单实例锁、Host 初始化或监听失败时透传 ``WorkspaceHostError`` 或系统异常。
    """

    args = _parse_args(argv)
    paths = WorkspacePaths.resolve(args.workspace)
    paths.prepare()
    singleton = WorkspaceHostLock(paths)
    singleton.acquire()
    token = ensure_local_token(paths)
    host = WorkspaceHost(
        paths,
        token,
        readiness_timeout=args.readiness_timeout,
        backend_port=args.backend_port,
    )
    server = ThreadingHTTPServer((args.host, args.port), _handler_type(host))
    endpoint = f"http://{args.host}:{server.server_address[1]}"
    host.publish_endpoint(endpoint)
    stopping = threading.Event()

    def stop_server(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is not None:
            signal.signal(value, stop_server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        host.close()
        singleton.release()



def _public_database_parameters(value: object) -> object:
    """操作响应和审计不回显数据库连接凭据；执行参数仅保存在 0600 操作文件中。"""
    if isinstance(value, dict):
        return {
            key: ("***" if key == "databaseUrl" and item else _public_database_parameters(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_database_parameters(item) for item in value]
    return value


if __name__ == "__main__":
    main()
