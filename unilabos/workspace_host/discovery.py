"""Single-host discovery, local authentication, and cross-platform locking."""

from __future__ import annotations

import os
import secrets
from typing import IO

from .model import WorkspaceHostError, WorkspacePaths, read_json


class WorkspaceHostLock:
    """Hold the per-workspace singleton lock for the complete Host lifetime."""

    def __init__(self, paths: WorkspacePaths) -> None:
        self._paths = paths
        self._stream: IO[bytes] | None = None

    def acquire(self) -> None:
        self._paths.prepare()
        stream = self._paths.lock.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            stream.close()
            details: object = None
            try:
                details = read_json(self._paths.session)
            except WorkspaceHostError:
                pass
            raise WorkspaceHostError(
                "host_already_running",
                "该 Workspace 已有 Workspace Host",
                details=details,
            ) from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n".encode("ascii"))
        stream.flush()
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> "WorkspaceHostLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def ensure_local_token(paths: WorkspacePaths) -> str:
    """Create or read a private bearer token scoped to this workspace."""

    paths.prepare()
    try:
        token = paths.token.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = secrets.token_hex(32)
        descriptor = os.open(paths.token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{token}\n")
    if not token:
        raise WorkspaceHostError("host_token_invalid", "Workspace Host token 为空")
    if os.name != "nt":
        os.chmod(paths.token, 0o600)
    return token
