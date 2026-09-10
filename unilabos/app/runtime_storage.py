"""集中管理 UniLab-OS 可重置运行态 SQLite 的启动生命周期。"""

from __future__ import annotations

import errno
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@dataclass(frozen=True)
class RuntimeStoragePaths:
    """一次启动使用的三类本地权威/投影存储路径。"""

    inventory_db: str
    device_state_db: str
    workflow_history_db: str


class RuntimeStorageInUseError(RuntimeError):
    """工作目录已被另一个 OS 进程持有时抛出的明确启动错误。"""


class _RuntimeDirectoryLock:
    """跨平台持有一个工作目录的进程级排他文件锁。"""

    def __init__(self, path: Path) -> None:
        """打开锁文件但不改变任何运行态数据库。

        参数：``path`` 是工作目录内固定的锁文件路径。返回：无。异常：文件无法
        打开时原样传播，调用者不得在未获得锁时清空数据库。
        """

        self._path = path
        self._handle: BinaryIO | None = path.open("a+b")
        self._acquired = False

    def acquire(self) -> None:
        """非阻塞取得排他锁。

        参数：无。返回：无。异常：锁冲突时抛出 ``RuntimeStorageInUseError``；
        其他文件系统错误原样传播。
        """

        if self._handle is None:
            raise RuntimeError("运行目录锁已经关闭")
        try:
            if os.name == "nt":
                self._handle.seek(0, os.SEEK_END)
                if self._handle.tell() == 0:
                    self._handle.write(b"\0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            self._acquired = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise RuntimeStorageInUseError(
                    f"运行目录已被另一个 OS 进程占用: {self._path.parent}"
                ) from error
            raise

    def close(self) -> None:
        """释放排他锁并关闭文件句柄。

        参数：无。返回：无；重复调用安全。异常：底层解锁或关闭错误原样传播。
        """

        handle = self._handle
        if handle is None:
            return
        if self._acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None
        self._acquired = False


@dataclass
class _RuntimeStorageSession:
    """持有一代 OS 的稳定目录锁与可丢弃数据库目录。"""

    working_root: Path
    runtime_root: Path
    paths: RuntimeStoragePaths
    preserve_databases: bool
    directory_lock: _RuntimeDirectoryLock
    temporary_directory: tempfile.TemporaryDirectory[str] | None
    closed: bool = False

    def discard(self) -> None:
        """按启动策略整体销毁本代私有临时目录。

        参数：无。返回：无；保留模式或已经销毁时不执行删除。异常：会话已关闭
        或临时目录清理失败时原样传播；稳定工作目录及其中旧库永不成为删除目标。
        """

        if self.closed:
            raise RuntimeError("运行态存储会话已经关闭")
        temporary_directory = self.temporary_directory
        if temporary_directory is None:
            return
        temporary_directory.cleanup()
        self.temporary_directory = None

    def close(self) -> None:
        """销毁私有运行态并释放稳定工作目录所有权。

        参数：无。返回：无；重复调用安全。异常：底层文件锁释放失败时原样传播，
        临时目录清理失败时不释放锁，避免下一代与残留连接并发。
        """

        if self.closed:
            return
        self.discard()
        self.directory_lock.close()
        self.closed = True


@dataclass(frozen=True)
class WorkingDirectoryResolution:
    """一次启动解析得到的可写运行目录及遗留目录命中状态。"""

    # ``path`` 是最终采用的绝对运行目录。
    path: str
    # ``used_legacy_directory`` 表示解析器自动复用了旧 ``unilabos_data``。
    used_legacy_directory: bool


_DEFAULT_WORKING_DIRECTORY_NAME = ".unilabos"
_LEGACY_WORKING_DIRECTORY_NAME = "unilabos_data"
_RUNTIME_LOCK_FILENAME = ".runtime-storage.lock"
_RUNTIME_TEMPORARY_PREFIX = "unilabos-runtime-"
_session_guard = threading.RLock()
_runtime_storage_session: _RuntimeStorageSession | None = None


def resolve_working_directory(
    *,
    requested: str | None,
    config_path: str | None,
    current_directory: str | Path | None = None,
) -> WorkingDirectoryResolution:
    """解析公共启动命令使用的唯一可写运行目录。

    参数：``requested`` 是显式或工作区（Workspace）派生的 ``working_dir``；
    ``config_path`` 是可选部署配置路径；``current_directory`` 是测试可覆盖的当前
    目录。返回：规范绝对路径以及是否自动复用了旧 ``unilabos_data`` 目录。
    异常：路径参数不是字符串/路径或为空时抛出 ``TypeError``/``ValueError``。

    显式/工作区路径始终精确优先，不再隐式追加子目录。没有请求路径时，新安装
    使用隐藏的 ``.unilabos``；仅当新目录不存在且旧目录已经存在时复用旧目录，
    防止一次升级静默切换本地持久事实。
    """

    if requested is not None:
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("working_dir 必须是非空路径")
        return WorkingDirectoryResolution(
            path=os.path.abspath(os.path.expanduser(requested)),
            used_legacy_directory=False,
        )

    if current_directory is None:
        base_directory = Path.cwd()
    elif isinstance(current_directory, (str, Path)):
        base_directory = Path(current_directory).expanduser()
    else:
        raise TypeError("current_directory 必须是字符串或 Path")
    base_directory = Path(os.path.abspath(base_directory))

    has_config = bool(config_path and os.path.exists(config_path))
    if has_config:
        base_directory = Path(os.path.dirname(os.path.abspath(str(config_path))))
    if base_directory.name == _DEFAULT_WORKING_DIRECTORY_NAME:
        return WorkingDirectoryResolution(str(base_directory), False)
    if base_directory.name == _LEGACY_WORKING_DIRECTORY_NAME:
        return WorkingDirectoryResolution(str(base_directory), True)

    preferred_directory = base_directory / _DEFAULT_WORKING_DIRECTORY_NAME
    legacy_directory = base_directory / _LEGACY_WORKING_DIRECTORY_NAME
    if preferred_directory.exists():
        return WorkingDirectoryResolution(str(preferred_directory), False)
    if legacy_directory.is_dir():
        return WorkingDirectoryResolution(str(legacy_directory), True)
    if has_config:
        return WorkingDirectoryResolution(str(base_directory), False)
    return WorkingDirectoryResolution(str(preferred_directory), False)


def resolve_runtime_storage_paths(
    arguments: dict[str, Any],
    *,
    working_dir: str,
) -> RuntimeStoragePaths:
    """从一个已选定的数据库目录补全三类本地 SQLite 存储路径。

    参数：``arguments`` 是公共命令行（CLI）参数；``working_dir`` 是组合根选定
    的稳定或临时数据库目录。
    返回：本轮启动唯一的运行时存储路径（RuntimeStoragePaths），并同步回参数字典。
    异常：参数形状或工作目录无效时抛出 ``TypeError``/``ValueError``。

    库存（Inventory）、设备状态与工作流历史不得分叉到不同目录；是否沿用稳定
    工作目录由 ``prepare_runtime_storage_session`` 统一决定。
    """

    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    if not isinstance(working_dir, str) or not working_dir.strip():
        raise ValueError("working_dir 必须是非空路径")

    # ``runtime_root`` 是三类本代运行事实的唯一目录边界。
    runtime_root = Path(working_dir).expanduser().resolve()
    resolved = RuntimeStoragePaths(
        inventory_db=str(runtime_root / "inventory.db"),
        device_state_db=str(runtime_root / "device_state.db"),
        workflow_history_db=str(runtime_root / "workflow_history.db"),
    )
    arguments["edge_inventory_db"] = resolved.inventory_db
    arguments["edge_device_state_db"] = resolved.device_state_db
    arguments["edge_workflow_history_db"] = resolved.workflow_history_db
    return resolved


def prepare_runtime_storage_session(
    arguments: dict[str, Any],
    *,
    working_dir: str,
) -> RuntimeStoragePaths:
    """在任何 Store 打开前取得目录锁并创建本代数据库目录。

    参数：``arguments`` 是公共命令行（CLI）参数，其中
    ``preserve_runtime_databases`` 必须为布尔值；``working_dir`` 是唯一运行
    目录。返回：默认位于新私有临时目录、显式保留时位于稳定工作目录的三类路径，
    并同步回参数字典。异常：并发进程占用目录时抛出
    ``RuntimeStorageInUseError``，策略、路径或临时目录创建无效时原样传播。相同
    会话的重复调用是幂等的，并返回同一个运行代际。
    """

    global _runtime_storage_session
    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    preserve_databases = arguments.get("preserve_runtime_databases", False)
    if not isinstance(preserve_databases, bool):
        raise TypeError("preserve_runtime_databases 必须是 bool")
    if not isinstance(working_dir, str) or not working_dir.strip():
        raise ValueError("working_dir 必须是非空路径")
    working_root = Path(working_dir).expanduser().resolve()

    with _session_guard:
        existing = _runtime_storage_session
        if existing is not None:
            if (
                existing.working_root != working_root
                or existing.preserve_databases != preserve_databases
            ):
                raise RuntimeError("同一进程不能切换活跃的运行态存储会话")
            return resolve_runtime_storage_paths(
                arguments,
                working_dir=str(existing.runtime_root),
            )

        working_root.mkdir(parents=True, exist_ok=True)
        directory_lock = _RuntimeDirectoryLock(working_root / _RUNTIME_LOCK_FILENAME)
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            directory_lock.acquire()
            if preserve_databases:
                runtime_root = working_root
            else:
                temporary_directory = tempfile.TemporaryDirectory(
                    prefix=_RUNTIME_TEMPORARY_PREFIX
                )
                runtime_root = Path(temporary_directory.name).resolve()
            paths = resolve_runtime_storage_paths(
                arguments,
                working_dir=str(runtime_root),
            )
            session = _RuntimeStorageSession(
                working_root=working_root,
                runtime_root=runtime_root,
                paths=paths,
                preserve_databases=preserve_databases,
                directory_lock=directory_lock,
                temporary_directory=temporary_directory,
            )
        except BaseException:
            if temporary_directory is not None:
                temporary_directory.cleanup()
            directory_lock.close()
            raise
        _runtime_storage_session = session
        return paths


def discard_runtime_storage_session() -> None:
    """整体销毁当前会话的私有临时数据库目录。

    参数：无。返回：无；尚未启动或显式保留模式时不操作。异常：SQLite 文件仍被
    占用或临时目录清理失败时原样传播，调用者应保留目录锁而不是启动下一代。
    """

    with _session_guard:
        if _runtime_storage_session is not None:
            _runtime_storage_session.discard()


def get_runtime_storage_directory() -> str | None:
    """返回当前三类可重置数据库共同使用的目录。

    参数：无。返回：会话已准备时返回稳定或私有临时目录，尚未准备时返回
    ``None``。异常：无；只读取组合根已经发布的会话身份。
    """

    with _session_guard:
        if _runtime_storage_session is None:
            return None
        return str(_runtime_storage_session.runtime_root)


def close_runtime_storage_session() -> None:
    """销毁私有临时目录并释放当前进程持有的工作目录锁。

    参数：无。返回：无；尚未启动或重复调用时安全。异常：解锁失败时原样传播，
    全局会话仍被保留以便诊断或重试。
    """

    global _runtime_storage_session
    with _session_guard:
        if _runtime_storage_session is None:
            return
        _runtime_storage_session.close()
        _runtime_storage_session = None


__all__ = [
    "RuntimeStorageInUseError",
    "RuntimeStoragePaths",
    "WorkingDirectoryResolution",
    "close_runtime_storage_session",
    "discard_runtime_storage_session",
    "get_runtime_storage_directory",
    "prepare_runtime_storage_session",
    "resolve_runtime_storage_paths",
    "resolve_working_directory",
]
