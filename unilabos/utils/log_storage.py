"""诊断日志的轮转、跨进程占用锁和历史清理（不接管业务数据）。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, fields
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping
from uuid import uuid4


@dataclass(frozen=True)
class LogPolicy:
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 9
    retention_days: float = 7
    total_max_bytes: int = 2 * 1024 * 1024 * 1024
    cleanup_interval_seconds: float = 600

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"日志配置 log_{field.name} 必须为正数")
            if field.name in {"max_bytes", "backup_count", "total_max_bytes"} and not isinstance(value, int):
                raise ValueError(f"日志配置 log_{field.name} 必须为正整数")

    @classmethod
    def from_config(cls, config: object) -> LogPolicy:
        defaults = cls()
        return cls(**{field.name: getattr(config, f"log_{field.name}", getattr(defaults, field.name)) for field in fields(cls)})

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LogPolicy:
        env = os.environ if env is None else env
        values: dict[str, int | float] = {}
        for field in fields(cls):
            key = f"UNILABOS_BASICCONFIG_LOG_{field.name.upper()}"
            if key in env:
                parser = int if field.name in {"max_bytes", "backup_count", "total_max_bytes"} else float
                try:
                    values[field.name] = parser(env[key])
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"日志配置 {key} 无效") from exc
        return cls(**values)


def cleanup_root_for(path: str | Path) -> Path:
    """Workbench 子运行目录共用工作区限额；显式工作目录独立管理。"""
    directory = Path(path).absolute()
    for candidate in (directory, *directory.parents):
        if candidate.name in {".unilabos", "unilabos_data"}:
            return candidate.resolve()
    return directory.resolve()


def unique_log_path(logs_dir: str | Path, prefix: str = "") -> Path:
    if Path(prefix).name != prefix and prefix:
        raise ValueError("日志前缀不能包含路径")
    suffix = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    return Path(logs_dir) / f"{prefix}{suffix}-{os.getpid()}-{uuid4().hex[:8]}.log"


class _FileLock:
    """锁文件不按 mtime 判断存活；进程退出后由内核释放占用。"""

    def __init__(self, path: Path, *, blocking: bool = False) -> None:
        self.path = path
        self.fd: int | None = None
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        if path.is_symlink():
            raise OSError(f"拒绝日志锁软链接: {path}")
        fd = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"日志锁不是普通文件: {path}")
            if os.name == "nt":
                import msvcrt

                # Windows 字节锁会阻止其他句柄读取同一区域。锁放在元数据之外，
                # 使清理器仍能识别活动会话并把其文件大小计入总量；允许锁在 EOF 后。
                os.lseek(fd, 4096, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self) -> _FileLock:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


_MARKER_SUFFIX = ".session.lock"
_FORMAT = "unilab-diagnostic-v1"


def _valid_marker(marker: Path, base: Path) -> bool:
    try:
        if marker.is_symlink() or marker.stat().st_size > 4096:
            return False
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        return metadata == {"format": _FORMAT, "file": base.name}
    except (OSError, ValueError):
        return False


def _inside_without_symlinks(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _segments(path: Path) -> list[Path]:
    pattern = re.compile(re.escape(path.name) + r"(?:\.[1-9][0-9]*)?$")
    try:
        return [entry for entry in path.parent.iterdir() if pattern.fullmatch(entry.name) and not entry.is_symlink() and entry.is_file()]
    except OSError:
        return []


def _warn(message: str, exc: BaseException) -> None:
    # 直接写原始终端，避免日志故障递归进入当前文件处理器。
    import sys

    try:
        stream = sys.__stderr__
        if stream is not None:
            stream.write(f"[日志维护] {message}: {exc}\n")
            stream.flush()
    except Exception:
        pass


def cleanup_logs(root: str | Path, policy: LogPolicy, *, now: float | None = None, blocking: bool = False) -> list[Path]:
    """仅删除拥有本模块标记且已释放占用锁的 .log 文件及轮转段。

    没有标记的旧版文件不会自动迁移或删除。活动日志计入总量，但不删除。
    """
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    removed: list[Path] = []
    try:
        directory_lock = _FileLock(root / ".unilab-log-cleanup.lock", blocking=blocking)
    except OSError:
        return removed
    locked: list[tuple[_FileLock, Path, Path]] = []
    try:
        total = 0
        candidates: list[tuple[float, int, Path]] = []
        for directory, subdirs, names in os.walk(root, followlinks=False):
            parent = Path(directory)
            subdirs[:] = [name for name in subdirs if name not in {".git", "node_modules", ".venv"} and not (parent / name).is_symlink()]
            for name in names:
                if not name.startswith(".") or not name.endswith(".log" + _MARKER_SUFFIX):
                    continue
                marker = parent / name
                if not _inside_without_symlinks(marker, root):
                    continue
                base = parent / name[1:-len(_MARKER_SUFFIX)]
                # 标记只声明同目录的精确文件名；不信任来自 JSON 的任意路径。
                if not _valid_marker(marker, base):
                    continue
                segments = _segments(base)
                try:
                    sizes = [(entry.stat().st_mtime, entry.stat().st_size, entry) for entry in segments]
                except OSError:
                    continue
                total += sum(size for _, size, _ in sizes)
                try:
                    session_lock = _FileLock(marker)
                except OSError:
                    continue
                locked.append((session_lock, marker, base))
                candidates.extend(sizes)
        for modified, size, path in sorted(candidates, key=lambda item: (item[0], str(item[2]))):
            if modified >= now - policy.retention_days * 86400 and total <= policy.total_max_bytes:
                continue
            try:
                if _inside_without_symlinks(path, root) and path.is_file():
                    path.unlink()
                    total -= size
                    removed.append(path)
            except OSError as exc:
                _warn(f"旧日志清理失败，稍后重试 {path}", exc)
        for session_lock, marker, base in locked:
            # Windows 不允许删除仍打开的文件。全局锁同时阻止新写入者注册。
            session_lock.close()
            if not _segments(base) and _inside_without_symlinks(marker, root):
                try:
                    marker.unlink(missing_ok=True)
                except OSError as exc:
                    _warn(f"日志占用标记清理失败 {marker}", exc)
    finally:
        for session_lock, _, _ in locked:
            session_lock.close()
        directory_lock.close()
    return removed


class _CleanupService:
    def __init__(self, root: Path, policy: LogPolicy) -> None:
        self.root = root
        self.policy = policy
        self.users = 0
        self.stop = False
        self.event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="diagnostic-log-cleanup", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                cleanup_logs(self.root, self.policy)
            except Exception as exc:
                _warn("历史日志检查失败，稍后重试", exc)
            if self.stop:
                return
            self.event.wait(self.policy.cleanup_interval_seconds)
            self.event.clear()


_services: dict[Path, _CleanupService] = {}
_services_lock = threading.Lock()


class _Session:
    def __init__(self, path: str | Path, policy: LogPolicy, cleanup_root: str | Path | None) -> None:
        path = Path(path).absolute()
        root_path = Path(cleanup_root).absolute() if cleanup_root is not None else path.parent
        if cleanup_root is None:
            for candidate in (path.parent, *path.parent.parents):
                if candidate.name in {".unilabos", "unilabos_data"}:
                    root_path = candidate
                    break
        if root_path.is_symlink() or not _inside_without_symlinks(path, root_path):
            raise ValueError("日志文件不能经过清理目录内的软链接")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.parent.resolve() / path.name
        if self.path.suffix != ".log" or self.path.is_symlink():
            raise ValueError("诊断日志必须是普通 .log 文件")
        self.root = root_path.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not _inside_without_symlinks(self.path, self.root):
            raise ValueError("日志文件必须位于清理目录内，且不能经过软链接")
        self.lock: _FileLock | None = None
        with _FileLock(self.root / ".unilab-log-cleanup.lock", blocking=True):
            marker = self.path.with_name(f".{self.path.name}{_MARKER_SUFFIX}")
            if self.path.exists() and not marker.exists():
                raise ValueError("旧版日志没有占用标记，请使用新会话文件；不可自动认领历史日志")
            if marker.exists() and not _valid_marker(marker, self.path):
                raise ValueError("日志占用标记无效，请使用新会话文件")
            self.lock = _FileLock(marker)
            try:
                content = json.dumps({"format": _FORMAT, "file": self.path.name}).encode("utf-8")
                assert self.lock.fd is not None
                os.lseek(self.lock.fd, 0, os.SEEK_SET)
                os.write(self.lock.fd, content)
                os.ftruncate(self.lock.fd, len(content))
            except BaseException:
                self.lock.close()
                raise
        with _services_lock:
            service = _services.get(self.root)
            if service is None:
                service = _CleanupService(self.root, policy)
                _services[self.root] = service
            service.users += 1
            service.policy = policy
            service.event.set()
            self.service = service

    def rotated(self) -> None:
        self.service.event.set()

    def close(self) -> None:
        if self.lock is None:
            return
        self.lock.close()
        self.lock = None
        with _services_lock:
            self.service.users -= 1
            if self.service.users == 0:
                self.service.stop = True
                _services.pop(self.root, None)
            self.service.event.set()


class SessionRotatingFileHandler(RotatingFileHandler):
    """每流独立占用锁；一条超长 Python 日志允许超过单文件阈值。"""

    def __init__(self, path: str | Path, policy: LogPolicy | None = None, cleanup_root: str | Path | None = None) -> None:
        self.policy = policy or LogPolicy.from_env()
        self._session = _Session(path, self.policy, cleanup_root)
        try:
            super().__init__(self._session.path, maxBytes=self.policy.max_bytes, backupCount=self.policy.backup_count, encoding="utf-8")
        except BaseException:
            self._session.close()
            raise

    def doRollover(self) -> None:
        super().doRollover()
        self._session.rotated()

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            # logger 的调用者可能已取到旧 handler，再等待重配关闭；不可重新开文件。
            if not self._closed:
                super().emit(record)
        finally:
            self.release()

    def close(self) -> None:
        self.acquire()
        try:
            try:
                super().close()
            finally:
                if hasattr(self, "_session"):
                    self._session.close()
        finally:
            self.release()


class RotatingByteWriter:
    """对子进程的任意字节块严格分段，不等换行，不持有无界缓冲。"""

    def __init__(self, path: str | Path, policy: LogPolicy | None = None, cleanup_root: str | Path | None = None) -> None:
        self.policy = policy or LogPolicy.from_env()
        self._session = _Session(path, self.policy, cleanup_root)
        self.path = self._session.path
        self._stream = None
        self._closed = False
        try:
            self._open()
        except BaseException:
            self._session.close()
            raise

    def _open(self) -> None:
        if self.path.is_symlink():
            raise OSError("拒绝写入日志软链接")
        self._stream = self.path.open("ab", buffering=0)

    def _rotate(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        oldest = self.path.with_name(f"{self.path.name}.{self.policy.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.policy.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists() and not source.is_symlink():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._open()
        self._session.rotated()

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ValueError("日志写入器已关闭")
        if self._stream is None or self._stream.closed:
            self._open()
        view = memoryview(data)
        while view:
            size = os.fstat(self._stream.fileno()).st_size
            if size >= self.policy.max_bytes:
                self._rotate()
                size = 0
            count = self._stream.write(view[: self.policy.max_bytes - size])
            if not count:
                raise OSError("日志写入未取得进展")
            view = view[count:]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._session.close()

    def __enter__(self) -> RotatingByteWriter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def read_log_tail(path: str | Path, max_bytes: int) -> bytes:
    """跨当前文件和最近轮转段读取尾部；总读入量受 max_bytes 限制。"""
    if max_bytes <= 0:
        return b""
    path = Path(path)
    segments = _segments(path)
    segments.sort(key=lambda entry: 0 if entry == path else int(entry.name[len(path.name) + 1 :]))
    chunks: list[bytes] = []
    remaining = max_bytes
    for entry in segments:
        if remaining <= 0:
            break
        try:
            with entry.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                stream.seek(max(0, size - remaining))
                chunk = stream.read(remaining)
        except OSError:
            continue
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(reversed(chunks))
