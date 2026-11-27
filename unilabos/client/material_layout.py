"""Public client for Workspace Host Material layout CAS operations."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

from unilabos.workspace_host.client import WorkspaceHostClient, ensure_workspace_host
from unilabos.workspace_host.model import WorkspaceHostError


class MaterialLayoutClient:
    """Small adapter shared by CLI and MCP; all mutation stays in Workspace Host."""

    def __init__(
        self,
        host: WorkspaceHostClient,
        *,
        operation_timeout: float = 120.0,
    ) -> None:
        self.host = host
        self.operation_timeout = operation_timeout

    @classmethod
    def discover(
        cls, workspace: str | Path, *, timeout: float = 120.0
    ) -> "MaterialLayoutClient":
        return cls(ensure_workspace_host(workspace), operation_timeout=timeout)

    def inspect(self) -> dict[str, Any]:
        return self._run("material.layout.inspect", {})

    def preview(
        self,
        change_set: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        return self._run(
            "material.layout.preview",
            {"changeSet": dict(change_set), "expectedRevision": expected_revision},
        )

    def apply(
        self,
        preview_id: str,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        return self._run(
            "material.layout.apply",
            {"previewId": preview_id, "expectedRevision": expected_revision},
        )

    def _run(self, command: str, parameters: Mapping[str, object]) -> dict[str, Any]:
        submitted = self.host.submit(
            command,
            parameters=dict(parameters),
            operation_id=str(uuid.uuid4()),
        )
        operation = self.host.wait(
            str(submitted["operationId"]), timeout=self.operation_timeout
        )
        if operation.get("phase") == "failed":
            failure = operation.get("error")
            if isinstance(failure, Mapping):
                raise WorkspaceHostError(
                    str(failure.get("code") or "operation_failed"),
                    str(failure.get("message") or "布局操作失败"),
                    details=failure.get("details"),
                )
            raise WorkspaceHostError("operation_failed", "布局操作失败")
        result = operation.get("result")
        if not isinstance(result, dict):
            raise WorkspaceHostError("operation_failed", "布局操作未返回结果")
        return result


__all__ = ["MaterialLayoutClient"]
