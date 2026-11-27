"""Public client for isolated workspace Material template validation."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

from unilabos.workspace_host.client import ensure_workspace_host
from unilabos.workspace_host.model import WorkspaceHostError


class MaterialTemplateClient:
    """Share one validation contract between CLI, MCP, and Workbench adapters."""

    def __init__(self, workspace: str | Path, *, timeout: float = 120.0) -> None:
        self.host = ensure_workspace_host(workspace)
        self.timeout = timeout

    def validate(self) -> dict[str, Any]:
        submitted = self.host.submit(
            "material.template.validate",
            operation_id=str(uuid.uuid4()),
        )
        operation = self.host.wait(
            str(submitted["operationId"]), timeout=self.timeout
        )
        if operation.get("phase") == "failed":
            failure = operation.get("error")
            if isinstance(failure, Mapping):
                raise WorkspaceHostError(
                    str(failure.get("code") or "template_validation_failed"),
                    str(failure.get("message") or "模板隔离编译失败"),
                    details=failure.get("details"),
                )
            raise WorkspaceHostError(
                "template_validation_failed", "模板隔离编译失败"
            )
        result = operation.get("result")
        if not isinstance(result, dict):
            raise WorkspaceHostError(
                "template_validation_failed", "模板隔离编译未返回结果"
            )
        return result


__all__ = ["MaterialTemplateClient"]
