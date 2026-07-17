"""基于 workflow sidecar 文件的 Task 工作区存储。"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable
from typing import Iterator

from .models import VersionedWorkspaceResponse, Workspace


class WorkflowPathError(ValueError):
    """workflow_path 不在服务允许的工作区内。"""


class VersionConflictError(ValueError):
    """客户端保存时使用了过期的版本号。"""


class SidecarSymlinkError(WorkflowPathError):
    """sidecar 不能是符号链接。"""


class SidecarCorruptionError(ValueError):
    """sidecar JSON 或其持久化模型无效。"""


class WorkspaceStore:
    """将每个 workflow 的 Task 数据存为相邻的 sidecar JSON 文件。"""

    def __init__(self, workspace_root: Path | str) -> None:
        self._root = Path(workspace_root).resolve()
        self._lock = threading.RLock()

    def get(self, workflow_path: str) -> VersionedWorkspaceResponse:
        """读取指定 workflow 的工作区；尚未保存时返回版本 0 的默认值。"""
        path, relative_path = self._resolve_workflow_path(workflow_path)
        sidecar = self._sidecar_path(path)
        with self._lock, self._sidecar_lock(sidecar, fcntl.LOCK_SH):
            return self._read_current(sidecar, relative_path)

    def put(
        self,
        workspace: Workspace,
        *,
        expected_version: int,
    ) -> VersionedWorkspaceResponse:
        """使用 expected_version 保存工作区，并原子替换 sidecar 文件。"""
        path, relative_path = self._resolve_workflow_path(workspace.workflow_path)
        sidecar = self._sidecar_path(path)
        with self._lock, self._sidecar_lock(sidecar, fcntl.LOCK_EX):
            current = self._read_current(sidecar, relative_path)
            if current.version != expected_version:
                raise VersionConflictError("workspace version conflict")
            result = VersionedWorkspaceResponse(
                version=current.version + 1,
                workspace=workspace.model_copy(update={"workflow_path": relative_path}),
            )
            self._write_json_atomically(sidecar, result.model_dump(mode="json"))
            return result

    def mutate(
        self,
        workflow_path: str,
        *,
        expected_version: int,
        operation: Callable[[Workspace], Workspace],
    ) -> VersionedWorkspaceResponse:
        """在同一 sidecar 排他锁内读取、更新并保存工作区。"""
        path, relative_path = self._resolve_workflow_path(workflow_path)
        sidecar = self._sidecar_path(path)
        with self._lock, self._sidecar_lock(sidecar, fcntl.LOCK_EX):
            current = self._read_current(sidecar, relative_path)
            if current.version != expected_version:
                raise VersionConflictError("workspace version conflict")
            updated = operation(current.workspace)
            result = VersionedWorkspaceResponse(
                version=current.version + 1,
                workspace=updated.model_copy(update={"workflow_path": relative_path}),
            )
            self._write_json_atomically(sidecar, result.model_dump(mode="json"))
            return result

    def _resolve_workflow_path(self, workflow_path: str) -> tuple[Path, str]:
        if not workflow_path or not workflow_path.strip():
            raise WorkflowPathError("workflow_path is required")
        raw_path = Path(workflow_path)
        candidate = raw_path if raw_path.is_absolute() else self._root / raw_path
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self._root)
        except ValueError as exc:
            raise WorkflowPathError("workflow_path escapes workspace root") from exc
        if not relative.parts:
            raise WorkflowPathError("workflow_path must name a file")
        return resolved, relative.as_posix()

    @staticmethod
    def _sidecar_path(workflow_path: Path) -> Path:
        return workflow_path.with_name(f"{workflow_path.name}.task-workspace.json")

    @staticmethod
    def _lock_path(sidecar_path: Path) -> Path:
        return sidecar_path.with_name(f".{sidecar_path.name}.lock")

    @contextmanager
    def _sidecar_lock(self, sidecar_path: Path, operation: int) -> Iterator[None]:
        lock_path = self._lock_path(sidecar_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise WorkflowPathError("workspace lock must not be a symlink") from exc
            raise
        try:
            fcntl.flock(descriptor, operation)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_current(
        self,
        sidecar_path: Path,
        relative_path: str,
    ) -> VersionedWorkspaceResponse:
        if sidecar_path.is_symlink():
            raise SidecarSymlinkError("workspace sidecar must not be a symlink")
        try:
            descriptor = os.open(
                sidecar_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return VersionedWorkspaceResponse(
                version=0,
                workspace=Workspace(workflow_path=relative_path),
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SidecarSymlinkError(
                    "workspace sidecar must not be a symlink"
                ) from exc
            raise
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                return VersionedWorkspaceResponse.model_validate_json(handle.read())
        except (json.JSONDecodeError, ValueError) as exc:
            raise SidecarCorruptionError("invalid task workspace sidecar") from exc

    @staticmethod
    def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
        if path.is_symlink():
            raise SidecarSymlinkError("workspace sidecar must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            WorkspaceStore._fsync_directory(path.parent)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
