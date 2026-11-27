"""Stable filesystem and wire contracts owned by a Workspace Host."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "unilab-workspace-host/v1"
COMPONENT_NAMES = ("backend", "edge", "plc", "renderer")


class WorkspaceHostError(RuntimeError):
    """A stable Workspace Host error suitable for CLI and HTTP clients."""

    def __init__(self, code: str, message: str, *, details: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class WorkspacePaths:
    """All mutable Workspace Host paths for one canonical workspace."""

    workspace: Path
    root: Path
    runtime: Path
    logs: Path
    operations: Path
    session: Path
    lock: Path
    token: Path
    audit: Path
    environment: Path

    @classmethod
    def resolve(cls, workspace: str | os.PathLike[str]) -> "WorkspacePaths":
        candidate = Path(workspace).expanduser().resolve()
        if not candidate.is_dir():
            raise WorkspaceHostError(
                "invalid_workspace",
                f"Workspace 不存在：{candidate}",
            )
        root = candidate / ".unilabos"
        runtime = root / "runtime" / "workbench"
        logs = root / "logs" / "workbench"
        return cls(
            workspace=candidate,
            root=root,
            runtime=runtime,
            logs=logs,
            operations=runtime / "operations",
            session=runtime / "session.json",
            lock=runtime / "host.lock",
            token=runtime / "host.token",
            audit=runtime / "audit.jsonl",
            environment=root / "environment.local.json",
        )

    def prepare(self) -> None:
        for directory in (self.root, self.runtime, self.logs, self.operations):
            directory.mkdir(parents=True, exist_ok=True)


def utc_timestamp() -> str:
    """Return a stable UTC timestamp without importing optional dependencies."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
    """Atomically persist one JSON document in the destination directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise WorkspaceHostError(
            "host_not_found", f"Workspace Host 未启动：{path}"
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceHostError(
            "host_state_invalid", f"Workspace Host 状态无效：{path}"
        ) from error
    if not isinstance(payload, dict):
        raise WorkspaceHostError(
            "host_state_invalid", f"Workspace Host 状态无效：{path}"
        )
    return payload


def idle_component(name: str) -> dict[str, object]:
    return {
        "name": name,
        "phase": "idle",
        "pid": None,
        "address": None,
        "generation": None,
        "logPath": None,
        "diagnostic": None,
        "capabilities": [],
    }


# Host 对外声明数据库参数处理能力，升级后的客户端不得猜测旧 Host 的行为。
DATABASE_SELECTION_CAPABILITY = "database-selection-v1"
STATION_FILE_CAPABILITY = "station-start-file-v1"
