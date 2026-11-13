"""一个数据库根目录只归一个进程：打开根目录里的任何库之前，先独占根目录下的锁文件。

- 锁是操作系统级的独占锁：POSIX 用 ``fcntl.lockf``（F_SETLK 记录锁），Windows 用
  ``msvcrt.locking``，锁的都是锁文件的第 0 字节。持锁进程无论怎么结束（正常退出、崩溃、
  ``kill -9``、``TerminateProcess``）都由操作系统释放，不需要清理陈旧锁；锁文件本身会留在
  目录里，它存在不代表被占用。
- 进程内可重入：同一进程对同一根目录重复 :func:`claim_database_root` 拿到的是同一把锁
  （启动入口、组合根与测试会在一个进程里多处打开同一根目录的库）。锁持有到进程退出，或显式
  :func:`release_database_root`。
- POSIX 记录锁属于进程而不属于文件描述符：本进程关闭指向锁文件的 **任何** 描述符都会释放它。
  所以锁文件只由本模块打开，持锁期间读持有者信息也只走持锁的那个描述符（见
  :func:`read_database_root_holder`）。记录锁也不随 ``fork`` 继承：持锁进程一退出锁就释放，
  不会被它派生的子进程延长。
- 不同根目录各自一把锁。默认拓扑里权威持有 ``<根目录>``，Host 子进程持有
  ``<根目录>/edge``，两把锁互不相干。

锁文件布局：第 0 字节是被锁的占位字节；从第 1 字节起是持有者信息（JSON：pid、启动时间、
角色、主机名、命令行）。Windows 的字节锁是强制锁，别的进程读不到第 0 字节，但能读到后面的
持有者信息，用来在冲突时说明是谁占着。
"""

from __future__ import annotations

import atexit
import errno
import json
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 根目录下的锁文件名
LOCK_FILENAME = "unilab.lock"
_LOCKED_BYTES = 1
_HOLDER_OFFSET = 1
_HOLDER_MAX_BYTES = 64 * 1024
_COMMAND_MAX_CHARS = 4000
#: 冲突提示里只显示命令行开头，完整命令行在锁文件里
_COMMAND_SHOWN_CHARS = 120
#: 等待锁释放（上一代进程刚退出）时的轮询间隔
_POLL_S = 0.1


class DatabaseRootLockError(RuntimeError):
    """数据库根目录无法加锁（目录不可写、文件系统不支持记录锁等）。"""


class DatabaseRootLocked(DatabaseRootLockError):
    """数据库根目录已被另一个进程独占。"""

    def __init__(self, root: Path, lock_path: Path, holder: Optional[dict[str, Any]]) -> None:
        self.root = root
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            f"数据库根目录 {root} 已被另一个 Uni-Lab-OS 进程占用（{describe_holder(holder)}）。"
            "一个数据库根目录同一时间只能由一个实例使用：请先停止该进程；要并行运行另一个实例，"
            f"请用 --working_dir 或 --server_database_root 指定另一个目录。锁文件：{lock_path}"
        )


@dataclass
class DatabaseRootLock:
    """本进程对一个数据库根目录的独占锁。"""

    root: Path
    path: Path
    holder: dict[str, Any]
    _fd: Optional[int] = field(default=None, repr=False)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        release_database_root(self.root)

    def _close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass  # 关闭描述符同样会释放
        finally:
            os.close(fd)


if os.name == "nt":
    import msvcrt

    _CONFLICT_ERRNOS = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}

    def _try_lock(fd: int) -> bool:
        # msvcrt.locking 从当前文件位置起锁；超出文件末尾的区域也可以锁
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCKED_BYTES)
        except OSError as exc:
            if exc.errno in _CONFLICT_ERRNOS:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCKED_BYTES)

else:
    import fcntl

    _CONFLICT_ERRNOS = {errno.EACCES, errno.EAGAIN}

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, _LOCKED_BYTES, 0, os.SEEK_SET)
        except OSError as exc:
            if exc.errno in _CONFLICT_ERRNOS:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.lockf(fd, fcntl.LOCK_UN, _LOCKED_BYTES, 0, os.SEEK_SET)


_guard = threading.Lock()
_held: dict[str, DatabaseRootLock] = {}
_atexit_registered = False


def _resolve_root(root: str | os.PathLike[str]) -> Path:
    text = str(root).strip()
    if not text:
        raise ValueError("database root cannot be empty")
    return Path(text).expanduser().resolve()


def _key(root: Path) -> str:
    return os.path.normcase(str(root))


def _read_holder(fd: int) -> Optional[dict[str, Any]]:
    try:
        os.lseek(fd, _HOLDER_OFFSET, os.SEEK_SET)
        raw = os.read(fd, _HOLDER_MAX_BYTES)
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None  # 持有者刚拿到锁、记录还没写完
    return data if isinstance(data, dict) else None


def _write_holder(fd: int, holder: dict[str, Any]) -> None:
    payload = b"\n" + json.dumps(holder, ensure_ascii=False).encode("utf-8") + b"\n"
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _command_line() -> str:
    return _shorten(" ".join([os.path.basename(sys.executable), *sys.argv]), _COMMAND_MAX_CHARS)


def describe_holder(holder: Optional[dict[str, Any]]) -> str:
    """把锁文件里的持有者记录写成一句话（冲突提示用）。"""

    if not holder:
        return "占用进程未知：锁文件里还没有可读的持有者记录"
    pid = holder.get("pid")
    host = str(holder.get("host") or "")
    who = f"pid {pid}" + (f"@{host}" if host and host != platform.node() else "")
    parts = [who]
    if holder.get("role"):
        parts.append(str(holder["role"]))
    if holder.get("started_at"):
        parts.append(f"{holder['started_at']} 启动")
    if holder.get("command"):
        parts.append(f"命令行 {_shorten(str(holder['command']), _COMMAND_SHOWN_CHARS)}")
    return "，".join(parts)


def _release_all_at_exit() -> None:
    # 操作系统在进程退出时同样会释放；显式解锁让被看护的下一代进程（安静点重启）立即拿到锁，
    # Windows 进程退出后释放字节锁可能有延迟。
    release_all_database_roots()


def claim_database_root(
    root: str | os.PathLike[str],
    *,
    role: str = "",
    wait_s: float = 0.0,
) -> DatabaseRootLock:
    """独占 ``root``；本进程已持有时直接返回同一把锁。

    Args:
        role: 写进持有者记录的进程角色，冲突提示里给对方看。
        wait_s: 被占用时最多等多久再放弃。顶层启动不等（第二个实例立即失败）；被看护的
            进程重新拉起时上一代刚退出，给操作系统一点释放时间。

    Raises:
        DatabaseRootLocked: 别的进程持有该根目录。
        DatabaseRootLockError: 根目录无法创建或锁文件无法加锁。
    """

    global _atexit_registered
    root_path = _resolve_root(root)
    key = _key(root_path)
    with _guard:
        existing = _held.get(key)
        if existing is not None:
            return existing
        lock_path = root_path / LOCK_FILENAME
        try:
            root_path.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as exc:
            raise DatabaseRootLockError(f"无法创建数据库根目录锁文件 {lock_path}: {exc}") from exc
        try:
            deadline = time.monotonic() + max(0.0, wait_s)
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise DatabaseRootLocked(root_path, lock_path, _read_holder(fd))
                time.sleep(_POLL_S)
        except DatabaseRootLocked:
            os.close(fd)
            raise
        except OSError as exc:
            os.close(fd)
            raise DatabaseRootLockError(
                f"无法给数据库根目录 {root_path} 加锁（{exc}）；根目录需在支持文件锁的本地磁盘上"
            ) from exc
        except BaseException:
            os.close(fd)
            raise
        holder = {
            "pid": os.getpid(),
            # 启动入口在打开任何库之前取锁，取锁时间即本实例的启动时间
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "role": role,
            "host": platform.node(),
            "command": _command_line(),
        }
        try:
            _write_holder(fd, holder)
        except OSError:
            pass  # 持有者记录只用于冲突提示，写不进去不影响独占
        lock = DatabaseRootLock(root_path, lock_path, holder, fd)
        _held[key] = lock
        if not _atexit_registered:
            atexit.register(_release_all_at_exit)
            _atexit_registered = True
        return lock


def release_database_root(root: str | os.PathLike[str]) -> bool:
    """释放本进程对 ``root`` 的锁；本进程没持有时返回 False。"""

    with _guard:
        lock = _held.pop(_key(_resolve_root(root)), None)
        if lock is None:
            return False
        lock._close()
        return True


def release_all_database_roots() -> None:
    with _guard:
        locks = list(_held.values())
        _held.clear()
        for lock in locks:
            lock._close()


def held_database_roots() -> list[Path]:
    """本进程当前持有的数据库根目录。"""

    with _guard:
        return [lock.root for lock in _held.values()]


def read_database_root_holder(root: str | os.PathLike[str]) -> Optional[dict[str, Any]]:
    """读 ``root`` 锁文件里的持有者记录；没有锁文件或记录不可读时返回 None。

    记录只说明最后一个持锁者是谁，不说明它是否还在运行——是否被占用以能否加锁为准。
    """

    root_path = _resolve_root(root)
    with _guard:
        lock = _held.get(_key(root_path))
        if lock is not None:
            # 本进程持锁：不能另开描述符再关闭，否则 POSIX 记录锁随之释放
            return dict(lock.holder)
        try:
            fd = os.open(root_path / LOCK_FILENAME, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError:
            return None
        try:
            return _read_holder(fd)
        finally:
            os.close(fd)


__all__ = [
    "DatabaseRootLock",
    "DatabaseRootLockError",
    "DatabaseRootLocked",
    "LOCK_FILENAME",
    "claim_database_root",
    "describe_holder",
    "held_database_roots",
    "read_database_root_holder",
    "release_all_database_roots",
    "release_database_root",
]
