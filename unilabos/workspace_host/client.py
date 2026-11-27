"""Recoverable Python client for the per-workspace Workspace Host."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from unilabos.utils.log_storage import LogPolicy
from unilabos.utils.process_output import ProcessOutput, session_log_path

from .model import (
    COMPONENT_NAMES,
    DATABASE_SELECTION_CAPABILITY,
    SCHEMA_VERSION,
    STATION_FILE_CAPABILITY,
    WorkspaceHostError,
    WorkspacePaths,
    read_json,
)

_OPERATION_TIMEOUT_SECONDS = 630.0


class WorkspaceHostClient:
    """Discover and call one Workspace Host without owning its processes."""

    def __init__(self, paths: WorkspacePaths, endpoint: str, token: str) -> None:
        self.paths = paths
        self.endpoint = endpoint.rstrip("/")
        self.token = token

    @classmethod
    def discover(cls, workspace: str | os.PathLike[str]) -> "WorkspaceHostClient":
        paths = WorkspacePaths.resolve(workspace)
        session = read_json(paths.session)
        if session.get("schemaVersion") != SCHEMA_VERSION:
            raise WorkspaceHostError(
                "host_state_invalid", "Workspace Host schema 不兼容"
            )
        host = session.get("host")
        if not isinstance(host, dict) or not isinstance(host.get("endpoint"), str):
            raise WorkspaceHostError(
                "host_state_invalid", "Workspace Host endpoint 缺失"
            )
        try:
            token = paths.token.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError) as error:
            raise WorkspaceHostError(
                "host_token_invalid", "Workspace Host token 不可读"
            ) from error
        return cls(paths, str(host["endpoint"]), token)

    @classmethod
    def status(cls, workspace: str | os.PathLike[str]) -> dict[str, Any]:
        """Return one stable snapshot even when the Host is offline.

        Read-only callers must not start a Host just to inspect a workspace.
        Keeping this fallback in the SDK gives CLI and MCP identical offline
        semantics instead of making each transport invent its own response.
        """

        paths = WorkspacePaths.resolve(workspace)
        try:
            return cls.discover(paths.workspace).snapshot(timeout=0.5)
        except WorkspaceHostError as error:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "workspacePath": str(paths.workspace),
                "host": {"phase": "offline", "pid": None, "endpoint": None},
                "components": {
                    name: {
                        "name": name,
                        "phase": "unknown",
                        "pid": None,
                        "address": None,
                        "generation": None,
                        "capabilities": [],
                    }
                    for name in COMPONENT_NAMES
                },
                "diagnostic": error.as_dict(),
            }

    def snapshot(self, *, timeout: float = 2.0) -> dict[str, Any]:
        return self._request("GET", "/v1/snapshot", timeout=timeout)

    def submit(
        self,
        command: str,
        *,
        parameters: dict[str, object] | None = None,
        operation_id: str | None = None,
        expected_revision: int | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        if parameters and {"stationFile", "databaseType", "databaseUrl"}.intersection(parameters):
            # 先检查能力再提交操作，避免旧 Host 忽略新参数却返回成功。
            host = self.snapshot(timeout=timeout).get("host", {})
            capabilities = host.get("capabilities", [])
            if "stationFile" in parameters and STATION_FILE_CAPABILITY not in capabilities:
                raise WorkspaceHostError("host_upgrade_required", "当前 Workspace Host 不支持工站启动文件；请退出旧 Host 并升级")
            if {"databaseType", "databaseUrl"}.intersection(parameters) and DATABASE_SELECTION_CAPABILITY not in capabilities:
                raise WorkspaceHostError(
                    "host_upgrade_required",
                    "当前 Workspace Host 不支持数据库选择，请先停止工作区并退出旧 Host，升级后重新启动；未提交启动操作",
                )
        payload: dict[str, object] = {
            "operationId": operation_id or str(uuid.uuid4()),
            "command": command,
            "parameters": parameters or {},
        }
        if expected_revision is not None:
            payload["expectedRevision"] = expected_revision
        return self._request("POST", "/v1/operations", payload, timeout=timeout)

    def operation(self, operation_id: str, *, timeout: float = 2.0) -> dict[str, Any]:
        return self._request("GET", f"/v1/operations/{operation_id}", timeout=timeout)

    def wait(
        self,
        operation_id: str,
        *,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest = self.operation(operation_id)
            if latest.get("phase") in {"succeeded", "failed"}:
                return latest
            time.sleep(0.1)
        raise WorkspaceHostError(
            "operation_timeout",
            f"等待操作完成超时：{operation_id}",
            details=latest,
        )

    def execute(
        self,
        command: str,
        *,
        parameters: dict[str, object] | None = None,
        operation_id: str | None = None,
        expected_revision: int | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Submit and await one idempotent Host operation.

        CLI, MCP, and UI adapters must share this failure normalization rather
        than each interpreting persisted operation documents independently.
        """

        submitted = self.submit(
            command,
            parameters=parameters,
            operation_id=operation_id,
            expected_revision=expected_revision,
        )
        accepted_id = submitted.get("operationId")
        if not isinstance(accepted_id, str) or not accepted_id:
            raise WorkspaceHostError(
                "host_protocol_invalid",
                "Workspace Host 操作未返回 operationId",
            )
        result = self.wait(accepted_id, timeout=timeout)
        if result.get("phase") != "failed":
            return result
        failure = result.get("error")
        if isinstance(failure, dict):
            raise WorkspaceHostError(
                str(failure.get("code") or "operation_failed"),
                str(failure.get("message") or "Workspace Host 操作失败"),
                details=failure.get("details"),
            )
        raise WorkspaceHostError("operation_failed", "Workspace Host 操作失败")

    def logs(self, component: str, *, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        return self._request("GET", f"/v1/logs/{component}?maxBytes={max_bytes}")

    def _request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.endpoint}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            try:
                failure = json.loads(error.read()).get("error", {})
            except (ValueError, AttributeError):
                failure = {}
            raise WorkspaceHostError(
                str(failure.get("code") or "host_request_failed"),
                str(failure.get("message") or f"Workspace Host HTTP {error.code}"),
                details=failure.get("details"),
            ) from error
        except (OSError, URLError, ValueError) as error:
            raise WorkspaceHostError(
                "host_unreachable",
                f"Workspace Host 不可达：{self.endpoint}",
            ) from error
        if not isinstance(result, dict):
            raise WorkspaceHostError("host_protocol_invalid", "Workspace Host 响应无效")
        return result


def ensure_workspace_host(
    workspace: str | os.PathLike[str],
    *,
    startup_timeout: float = 10.0,
) -> WorkspaceHostClient:
    """Return the live Host or start a detached Host for this workspace."""

    paths = WorkspacePaths.resolve(workspace)
    try:
        existing = WorkspaceHostClient.discover(paths.workspace)
        existing.snapshot(timeout=0.5)
        return existing
    except WorkspaceHostError:
        pass
    paths.prepare()
    log_path = session_log_path(paths.logs / "workspace-host.log")
    environment = dict(os.environ)
    checkout = Path(__file__).resolve().parents[2]
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(checkout), inherited) if part
    )
    command = [
        sys.executable,
        "-m",
        "unilabos.workspace_host.host",
        "--workspace",
        str(paths.workspace),
        "--port",
        "0",
    ]
    with ProcessOutput(log_path, LogPolicy.from_env(environment)) as output:
        subprocess.Popen(
            command,
            cwd=paths.workspace,
            env=output.environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=output.stream,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
    deadline = time.monotonic() + startup_timeout
    last_error: WorkspaceHostError | None = None
    while time.monotonic() < deadline:
        try:
            client = WorkspaceHostClient.discover(paths.workspace)
            client.snapshot(timeout=0.5)
            return client
        except WorkspaceHostError as error:
            last_error = error
            time.sleep(0.05)
    raise WorkspaceHostError(
        "host_start_failed",
        f"Workspace Host 未能启动；日志：{log_path}",
        details=None if last_error is None else last_error.as_dict(),
    )
