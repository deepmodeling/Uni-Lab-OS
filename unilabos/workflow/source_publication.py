"""工作流源码（Workflow Source）的可移植原子 CAS 发布内部实现。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import stat
import struct
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    assert_directory_identity,
    binary_open_flags,
    directory_identity,
    is_reparse_point,
    read_regular_path,
    read_stable_descriptor,
)
from unilabos.workflow.source_publication_errors import (
    SourcePublicationConflict,
    SourcePublicationError,
    raise_classified_lock_os_error,
    raise_classified_publication_os_error,
)
from unilabos.workflow.source_windows_publication import (
    WindowsPublicationConflict,
    WindowsPublicationError,
    hold_windows_directory_chain,
    replace_windows_file_with_backup,
    restore_missing_windows_target,
    verify_windows_backup_or_restore,
    windows_directory_chain,
)

try:  # pragma: no cover - 导入分支由目标操作系统决定
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows 没有 fcntl
    _fcntl = None

try:  # pragma: no cover - 导入分支由目标操作系统决定
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX 没有 msvcrt
    _msvcrt = None

NO_EXPECTED_HASH = object()
_PLATFORM = sys.platform
_F_OWNER_TID = 0
_LEASE_BREAK_SIGNAL = getattr(signal, "SIGRTMAX", None)


@dataclass(frozen=True)
class _PublicationDirectory:
    """统一目录描述符与安全绝对路径两种发布后端。"""

    descriptor: int | None
    path: Path | None
    identity: tuple[int, int]

    @classmethod
    def create(
        cls,
        *,
        parent_descriptor: int | None,
        parent_path: str | Path | None,
    ) -> _PublicationDirectory:
        """校验并固定恰好一种父目录访问方式。

        参数：``parent_descriptor`` 是支持 ``dir_fd`` 平台的固定目录；
        ``parent_path`` 是无 ``dir_fd`` 平台的规范绝对目录。返回：统一发布目录。
        异常：两者同时提供、同时缺失或目录不安全时抛出 ``SourcePublicationError``。
        """

        if (parent_descriptor is None) == (parent_path is None):
            raise SourcePublicationError("publication_failed")
        try:
            if parent_descriptor is not None:
                metadata = os.fstat(parent_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SourcePublicationError("publication_failed")
                return cls(
                    descriptor=parent_descriptor,
                    path=None,
                    identity=(metadata.st_dev, metadata.st_ino),
                )
            absolute_path = Path(os.path.abspath(Path(parent_path)))
            return cls(
                descriptor=None,
                path=absolute_path,
                identity=directory_identity(absolute_path),
            )
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise SourcePublicationError("publication_failed") from None

    def assert_current(self) -> None:
        """复核发布父目录仍是创建时固定的同一物理目录。

        参数：无。返回：身份不变时无返回值。异常：替换或不可访问时抛出
        ``SourcePublicationError``。
        """

        try:
            if self.descriptor is not None:
                metadata = os.fstat(self.descriptor)
                current = metadata.st_dev, metadata.st_ino
                if not stat.S_ISDIR(metadata.st_mode) or current != self.identity:
                    raise SourcePublicationError("publication_failed")
            else:
                assert self.path is not None
                assert_directory_identity(self.path, self.identity)
        except (OSError, StableFileAccessError):
            raise SourcePublicationError("publication_failed") from None

    def open_child(self, name: str, flags: int, mode: int = 0o777) -> int:
        """在固定父目录中打开一个单段子文件。

        参数：``name`` 是单段文件名；``flags`` 和 ``mode`` 与 ``os.open`` 一致。
        返回：调用者负责关闭的描述符。异常：目录身份变化或打开失败原样交给上层。
        """

        self.assert_current()
        if self.descriptor is not None:
            return os.open(
                name,
                binary_open_flags(flags),
                mode,
                dir_fd=self.descriptor,
            )
        assert self.path is not None
        return os.open(self.path / name, binary_open_flags(flags), mode)

    def stat_child(self, name: str) -> os.stat_result:
        """不跟随符号链接读取一个单段子文件元数据。

        参数：``name`` 是目标文件名。返回：``lstat`` 等价元数据；目录身份在读取
        前复核，系统错误原样交给调用者。
        """

        self.assert_current()
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        assert self.path is not None
        return (self.path / name).lstat()

    def replace_child(self, source_name: str, target_name: str) -> None:
        """原子替换同一固定目录内的目标文件。

        参数：``source_name`` 是完整临时文件；``target_name`` 是规范文件。返回：
        无；目录身份变化或替换失败原样交给上层。
        """

        self.assert_current()
        if self.descriptor is not None:
            os.replace(
                source_name,
                target_name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
            return
        assert self.path is not None
        os.replace(self.path / source_name, self.path / target_name)

    def link_child(self, source_name: str, target_name: str) -> None:
        """以“不覆盖已存在目标”的硬链接完成首次发布。

        参数：``source_name`` 是临时文件；``target_name`` 是缺失的规范文件。
        返回：无；并发创建表现为 ``FileExistsError``。
        """

        self.assert_current()
        if self.descriptor is not None:
            os.link(
                source_name,
                target_name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
                follow_symlinks=False,
            )
            return
        assert self.path is not None
        os.link(
            self.path / source_name,
            self.path / target_name,
            follow_symlinks=False,
        )

    def unlink_child(self, name: str) -> None:
        """删除固定目录中的发布临时文件。

        参数：``name`` 是单段临时名。返回：无；目录身份变化或删除失败原样抛出。
        """

        self.assert_current()
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
            return
        assert self.path is not None
        os.unlink(self.path / name)

    def sync(self) -> None:
        """尽平台能力同步父目录元数据。

        参数：无。返回：无；POSIX 描述符必须成功 ``fsync``，Windows 绝对路径
        后端在系统不允许打开/同步目录时采用原子替换保证并安全降级。
        """

        self.assert_current()
        if self.descriptor is not None:
            os.fsync(self.descriptor)
            return
        assert self.path is not None
        directory_descriptor = -1
        try:
            directory_descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(directory_descriptor)
        except OSError:
            if not _PLATFORM.startswith("win"):
                raise
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)


def atomic_publish_source(
    *,
    parent_descriptor: int | None = None,
    parent_path: str | Path | None = None,
    target_name: str,
    content: bytes,
    byte_limit: int,
    expected_hash: object | str | None = NO_EXPECTED_HASH,
) -> None:
    """在固定目录中原子发布一份工作流源码（Workflow Source）。

    参数：父目录描述符或绝对路径必须恰好提供一种；``target_name`` 是单段文件名；
    ``content`` 是待发布字节；``byte_limit`` 是硬上限；``expected_hash`` 是可选
    SHA-256 CAS 条件。返回：成功时规范路径指向完整新文件。异常：并发变化抛出
    ``SourcePublicationConflict``；平台能力或文件系统失败抛出
    ``SourcePublicationError``。
    """

    if (
        len(content) > byte_limit
        or not target_name
        or Path(target_name).name != target_name
    ):
        raise SourcePublicationError("publication_failed")
    location = _PublicationDirectory.create(
        parent_descriptor=parent_descriptor,
        parent_path=parent_path,
    )
    temporary_name = f".{target_name}.{uuid4().hex}.tmp"
    if _PLATFORM.startswith("win"):
        if location.path is None:
            raise SourcePublicationError("publication_failed")
        directory_guard = hold_windows_directory_chain(
            windows_directory_chain(location.path)
        )
    else:
        directory_guard = nullcontext()
    try:
        with directory_guard:
            location.assert_current()
            descriptor = -1
            try:
                descriptor = location.open_child(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected_hash is NO_EXPECTED_HASH:
                    location.replace_child(temporary_name, target_name)
                else:
                    _compare_and_replace(
                        location=location,
                        target_name=target_name,
                        temporary_name=temporary_name,
                        expected_hash=expected_hash,
                        byte_limit=byte_limit,
                    )
                location.sync()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError, OSError, SourcePublicationError):
                    location.unlink_child(temporary_name)
            location.assert_current()
    except SourcePublicationConflict:
        raise
    except WindowsPublicationConflict:
        raise SourcePublicationConflict("draft_hash_conflict") from None
    except WindowsPublicationError:
        raise SourcePublicationError("publication_failed") from None
    except OSError as error:
        raise_classified_publication_os_error(error)
    except StableFileAccessError:
        raise SourcePublicationError("publication_failed") from None


def _compare_and_replace(
    *,
    location: _PublicationDirectory,
    target_name: str,
    temporary_name: str,
    expected_hash: str | None,
    byte_limit: int,
) -> None:
    """在平台可用独占锁下执行受限读取、比较与原子替换。

    参数：``location`` 固定发布目录；目标名和临时名标识两份文件；
    ``expected_hash`` 是原稿条件；``byte_limit`` 约束所有内容读取。返回：CAS 成功
    时无返回值。异常：锁竞争、身份变化或内容冲突抛出
    ``SourcePublicationConflict``；权限、空间和 I/O 故障抛出
    ``SourcePublicationError``。
    """

    if _PLATFORM.startswith("win"):
        _compare_and_replace_windows(
            location=location,
            target_name=target_name,
            temporary_name=temporary_name,
            expected_hash=expected_hash,
            byte_limit=byte_limit,
        )
        return
    if _PLATFORM.startswith("darwin"):
        _compare_and_replace_darwin(
            location=location,
            target_name=target_name,
            temporary_name=temporary_name,
            expected_hash=expected_hash,
            byte_limit=byte_limit,
        )
        return

    target_descriptor = -1
    temporary_descriptor = -1
    try:
        try:
            target_descriptor = location.open_child(
                target_name,
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            if expected_hash is not None:
                raise SourcePublicationConflict("draft_hash_conflict") from None
            try:
                location.link_child(temporary_name, target_name)
            except FileExistsError:
                raise SourcePublicationConflict("draft_hash_conflict") from None
            location.unlink_child(temporary_name)
            return
        if expected_hash is None:
            raise SourcePublicationConflict("draft_hash_conflict")

        with _exclusive_target_lock(target_descriptor) as lock_was_broken:
            original_bytes = _stable_bytes(
                target_descriptor,
                byte_limit=byte_limit,
            )
            if (
                _sha256(original_bytes) != expected_hash
                or not _target_matches_descriptor(
                    location,
                    target_name,
                    target_descriptor,
                )
            ):
                raise SourcePublicationConflict("draft_hash_conflict")
            temporary_descriptor = location.open_child(
                temporary_name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            replacement_hash = _sha256(
                _stable_bytes(temporary_descriptor, byte_limit=byte_limit)
            )
            if lock_was_broken() or not _target_matches_descriptor(
                location,
                target_name,
                target_descriptor,
            ):
                raise SourcePublicationConflict("draft_hash_conflict")
            location.replace_child(temporary_name, target_name)
            location.sync()
            if lock_was_broken() or not _published_identity_matches(
                location=location,
                target_name=target_name,
                published_descriptor=temporary_descriptor,
                expected_hash=replacement_hash,
                byte_limit=byte_limit,
            ):
                raise SourcePublicationConflict("draft_hash_conflict")
        if not _published_identity_matches(
            location=location,
            target_name=target_name,
            published_descriptor=temporary_descriptor,
            expected_hash=replacement_hash,
            byte_limit=byte_limit,
        ):
            raise SourcePublicationConflict("draft_hash_conflict")
    except (SourcePublicationConflict, SourcePublicationError):
        raise
    except OSError as error:
        raise_classified_publication_os_error(error)
    except StableFileAccessError:
        raise SourcePublicationConflict("draft_hash_conflict") from None
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)


def _compare_and_replace_darwin(
    *,
    location: _PublicationDirectory,
    target_name: str,
    temporary_name: str,
    expected_hash: str | None,
    byte_limit: int,
) -> None:
    """用 Darwin ``RENAME_SWAP`` 证据协议完成已有草稿 CAS。

    参数：目录、目标名和临时名固定同目录中的两份文件；``expected_hash`` 是
    调用者读取的原稿身份；``byte_limit`` 限制所有验证读取。返回：成功时规范
    路径指向新稿、临时路径保留已验证旧稿并由外层清理。异常：并发变化回滚后
    抛出 ``SourcePublicationConflict``；无法证明安全回滚时抛出发布错误。
    """

    target_descriptor = -1
    temporary_descriptor = -1
    swapped = False
    try:
        try:
            target_descriptor = location.open_child(
                target_name,
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            if expected_hash is not None:
                raise SourcePublicationConflict("draft_hash_conflict") from None
            try:
                location.link_child(temporary_name, target_name)
            except FileExistsError:
                raise SourcePublicationConflict("draft_hash_conflict") from None
            location.unlink_child(temporary_name)
            return
        if expected_hash is None:
            raise SourcePublicationConflict("draft_hash_conflict")

        with _exclusive_target_lock(target_descriptor):
            original_bytes = _stable_bytes(
                target_descriptor,
                byte_limit=byte_limit,
            )
            if (
                _sha256(original_bytes) != expected_hash
                or not _target_matches_descriptor(
                    location,
                    target_name,
                    target_descriptor,
                )
            ):
                raise SourcePublicationConflict("draft_hash_conflict")
            temporary_descriptor = location.open_child(
                temporary_name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            replacement_hash = _sha256(
                _stable_bytes(temporary_descriptor, byte_limit=byte_limit)
            )

            _darwin_swap_children(location, target_name, temporary_name)
            swapped = True
            location.sync()
            original_was_swapped = _published_identity_matches(
                location=location,
                target_name=temporary_name,
                published_descriptor=target_descriptor,
                expected_hash=expected_hash,
                byte_limit=byte_limit,
            )
            replacement_was_published = _published_identity_matches(
                location=location,
                target_name=target_name,
                published_descriptor=temporary_descriptor,
                expected_hash=replacement_hash,
                byte_limit=byte_limit,
            )
            if not original_was_swapped or not replacement_was_published:
                replacement_still_owns_target = _target_matches_descriptor(
                    location,
                    target_name,
                    temporary_descriptor,
                )
                if replacement_still_owns_target:
                    _darwin_swap_children(location, target_name, temporary_name)
                    swapped = False
                    location.sync()
                raise SourcePublicationConflict("draft_hash_conflict")
    except (SourcePublicationConflict, SourcePublicationError):
        raise
    except OSError as error:
        if swapped:
            raise SourcePublicationError("publication_failed") from None
        raise_classified_publication_os_error(error)
    except StableFileAccessError:
        raise SourcePublicationConflict("draft_hash_conflict") from None
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)


def _darwin_swap_children(
    location: _PublicationDirectory,
    first_name: str,
    second_name: str,
) -> None:
    """通过 libc ``renameatx_np(..., RENAME_SWAP)`` 原子交换同目录文件。

    参数：``location`` 固定父目录；两个名称都是已校验的单段子文件名。返回：
    成功时两条目录项原子交换。异常：缺少 Darwin API 或系统调用失败时抛出
    ``SourcePublicationError``/``OSError``，调用者据交换状态决定错误分类。
    """

    if not _PLATFORM.startswith("darwin"):
        raise SourcePublicationError("publication_failed")
    location.assert_current()
    directory_descriptor = location.descriptor
    opened_descriptor = -1
    if directory_descriptor is None:
        assert location.path is not None
        opened_descriptor = os.open(
            location.path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        directory_descriptor = opened_descriptor
        metadata = os.fstat(directory_descriptor)
        if (metadata.st_dev, metadata.st_ino) != location.identity:
            os.close(opened_descriptor)
            raise SourcePublicationError("publication_failed")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renameatx_np = libc.renameatx_np
        except AttributeError:
            raise SourcePublicationError("publication_failed") from None
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            directory_descriptor,
            os.fsencode(first_name),
            directory_descriptor,
            os.fsencode(second_name),
            0x00000002,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    finally:
        if opened_descriptor >= 0:
            os.close(opened_descriptor)


def _compare_and_replace_windows(
    *,
    location: _PublicationDirectory,
    target_name: str,
    temporary_name: str,
    expected_hash: str | None,
    byte_limit: int,
) -> None:
    """用 Windows 字节锁、目录 guard 和 ``ReplaceFileW`` 完成已有草稿 CAS。

    参数：``location`` 必须是无 ``dir_fd`` 的固定绝对父目录；两个文件名标识规范
    草稿与完整临时稿；``expected_hash`` 是原稿条件；``byte_limit`` 约束每次读取。
    返回：成功时无返回值。异常：竞争映射为 ``SourcePublicationConflict``，无法
    证明回滚的基础设施故障映射为 ``SourcePublicationError``。
    """

    if location.path is None or _msvcrt is None:
        raise SourcePublicationConflict("draft_hash_conflict")
    target_path = location.path / target_name
    temporary_path = location.path / temporary_name
    backup_path = location.path / f".{target_name}.{uuid4().hex}.cas"
    target_descriptor = -1
    temporary_descriptor = -1
    try:
        with hold_windows_directory_chain(windows_directory_chain(location.path)):
            location.assert_current()
            try:
                target_descriptor = location.open_child(
                    target_name,
                    os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                )
            except FileNotFoundError:
                if expected_hash is not None:
                    raise SourcePublicationConflict("draft_hash_conflict") from None
                try:
                    location.link_child(temporary_name, target_name)
                except FileExistsError:
                    raise SourcePublicationConflict("draft_hash_conflict") from None
                location.unlink_child(temporary_name)
                return
            if expected_hash is None:
                raise SourcePublicationConflict("draft_hash_conflict")

            with _exclusive_target_lock(target_descriptor) as lock_was_broken:
                original_bytes = _stable_bytes(
                    target_descriptor,
                    byte_limit=byte_limit,
                )
                if (
                    _sha256(original_bytes) != expected_hash
                    or not _target_matches_descriptor(
                        location,
                        target_name,
                        target_descriptor,
                    )
                ):
                    raise SourcePublicationConflict("draft_hash_conflict")
                temporary_descriptor = location.open_child(
                    temporary_name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
                replacement_hash = _sha256(
                    _stable_bytes(temporary_descriptor, byte_limit=byte_limit)
                )
                if lock_was_broken() or not _target_matches_descriptor(
                    location,
                    target_name,
                    target_descriptor,
                ):
                    raise SourcePublicationConflict("draft_hash_conflict")

            # Windows CRT 句柄默认不共享 delete；原稿与替换稿都必须在原生替换前
            # 解锁并关闭。替换瞬间的 backup 随后重新证明 CAS 原稿身份。
            os.close(target_descriptor)
            target_descriptor = -1
            os.close(temporary_descriptor)
            temporary_descriptor = -1
            current_snapshot = read_regular_path(
                target_path,
                byte_limit=byte_limit,
                missing_ok=False,
            )
            if (
                current_snapshot is None
                or _sha256(current_snapshot.content) != expected_hash
            ):
                raise SourcePublicationConflict("draft_hash_conflict")
            location.assert_current()
            replace_windows_file_with_backup(
                target_path,
                temporary_path,
                backup_path,
            )
            verify_windows_backup_or_restore(
                parent=location.path,
                target=target_path,
                backup=backup_path,
                expected_hash=expected_hash,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )
            published = read_regular_path(
                target_path,
                byte_limit=byte_limit,
                missing_ok=False,
            )
            if published is None or _sha256(published.content) != replacement_hash:
                raise SourcePublicationError("publication_failed")
            location.assert_current()
    except SourcePublicationConflict:
        raise
    except WindowsPublicationConflict:
        if not restore_missing_windows_target(
            target=target_path,
            backup=backup_path,
        ):
            raise SourcePublicationError("publication_failed") from None
        raise SourcePublicationConflict("draft_hash_conflict") from None
    except (WindowsPublicationError, StableFileAccessError):
        raise SourcePublicationError("publication_failed") from None
    except OSError as error:
        raise_classified_publication_os_error(error)
    except (TypeError, ValueError):
        raise SourcePublicationError("publication_failed") from None
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)


@contextmanager
def _exclusive_target_lock(
    descriptor: int,
) -> Iterator[Callable[[], bool]]:
    """按 Linux、macOS/POSIX、Windows 顺序选择目标文件独占锁。

    参数：``descriptor`` 是 CAS 原稿文件。返回：上下文值是“是否观察到 Linux
    租约中断”的无参函数；其他平台恒为 ``False``。异常：锁已被占用时抛出
    ``SourcePublicationConflict``；锁基础设施故障抛出
    ``SourcePublicationError``，两种情况都绝不无锁继续发布。
    """

    if _linux_lease_supported():
        with _linux_lease(descriptor) as lease_broken:
            yield lease_broken
        return
    if _fcntl is not None and all(
        hasattr(_fcntl, name) for name in ("flock", "LOCK_EX", "LOCK_NB", "LOCK_UN")
    ):
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as error:
            raise_classified_lock_os_error(error)
        except ValueError:
            raise SourcePublicationError("publication_failed") from None
        try:
            yield _never_broken
        finally:
            with suppress(OSError, ValueError):
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None and all(
        hasattr(_msvcrt, name) for name in ("locking", "LK_NBLCK", "LK_UNLCK")
    ):
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            _msvcrt.locking(descriptor, _msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise_classified_lock_os_error(error)
        except ValueError:
            raise SourcePublicationError("publication_failed") from None
        try:
            yield _never_broken
        finally:
            with suppress(OSError, ValueError):
                os.lseek(descriptor, 0, os.SEEK_SET)
                _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
        return
    raise SourcePublicationConflict("draft_hash_conflict")


def _linux_lease_supported() -> bool:
    """判断当前运行时是否完整提供 Linux 可中断写租约。

    参数：无。返回：平台、``fcntl`` 常量、实时信号和线程信号 API 均存在时为
    ``True``；macOS 仅有 ``flock`` 时为 ``False``。
    """

    return bool(
        _PLATFORM.startswith("linux")
        and _fcntl is not None
        and _LEASE_BREAK_SIGNAL is not None
        and all(
            hasattr(_fcntl, name)
            for name in (
                "fcntl",
                "F_SETSIG",
                "F_SETLEASE",
                "F_WRLCK",
                "F_UNLCK",
            )
        )
        and hasattr(signal, "pthread_sigmask")
        and hasattr(signal, "sigtimedwait")
    )


@contextmanager
def _linux_lease(descriptor: int) -> Iterator[Callable[[], bool]]:
    """获取可检测外部打开的 Linux 写租约并在退出时完整恢复信号状态。

    参数：``descriptor`` 是 CAS 原稿。返回：租约中断探测函数。异常：明确的
    锁竞争抛出 ``SourcePublicationConflict``；租约或信号基础设施故障抛出
    ``SourcePublicationError``。
    """

    assert _fcntl is not None
    assert _LEASE_BREAK_SIGNAL is not None
    previous_signal_mask: set[signal.Signals] | None = None
    lease_held = False
    try:
        previous_signal_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {_LEASE_BREAK_SIGNAL},
        )
        owner_operation = getattr(_fcntl, "F_SETOWN_EX", 15)
        _fcntl.fcntl(
            descriptor,
            owner_operation,
            struct.pack("ii", _F_OWNER_TID, threading.get_native_id()),
        )
        _fcntl.fcntl(descriptor, _fcntl.F_SETSIG, _LEASE_BREAK_SIGNAL)
        _fcntl.fcntl(descriptor, _fcntl.F_SETLEASE, _fcntl.F_WRLCK)
        lease_held = True
        yield _drain_lease_break_signal
    except SourcePublicationConflict:
        raise
    except OSError as error:
        raise_classified_publication_os_error(error)
    except (AttributeError, ValueError):
        raise SourcePublicationError("publication_failed") from None
    finally:
        if lease_held:
            with suppress(OSError):
                _fcntl.fcntl(descriptor, _fcntl.F_SETLEASE, _fcntl.F_UNLCK)
        if previous_signal_mask is not None:
            with suppress(OSError, ValueError):
                _drain_lease_break_signal()
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)


def _published_identity_matches(
    *,
    location: _PublicationDirectory,
    target_name: str,
    published_descriptor: int,
    expected_hash: str,
    byte_limit: int,
) -> bool:
    """验证规范路径仍指向本次发布的文件身份与内容。

    参数：目录、目标名和发布描述符固定本次结果；哈希与上限约束内容。返回：
    身份和稳定内容均匹配时为 ``True``；确定的文件世代变化为 ``False``。
    异常：权限、空间或 I/O 等基础设施故障原样传播给上层分类，不能伪装为冲突。
    """

    try:
        return _target_matches_descriptor(
            location,
            target_name,
            published_descriptor,
        ) and _sha256(
            _stable_bytes(published_descriptor, byte_limit=byte_limit)
        ) == expected_hash
    except SourcePublicationConflict:
        return False


def _target_matches_descriptor(
    location: _PublicationDirectory,
    target_name: str,
    descriptor: int,
) -> bool:
    """比较规范路径与已打开描述符的物理普通文件身份。

    参数：``location`` 和 ``target_name`` 指定路径；``descriptor`` 指定已打开
    文件。返回：两者是同一普通文件时为 ``True``，目标缺失时为 ``False``。
    """

    try:
        target_metadata = location.stat_child(target_name)
    except FileNotFoundError:
        return False
    descriptor_metadata = os.fstat(descriptor)
    return (
        stat.S_ISREG(target_metadata.st_mode)
        and not is_reparse_point(target_metadata)
        and stat.S_ISREG(descriptor_metadata.st_mode)
        and target_metadata.st_dev == descriptor_metadata.st_dev
        and target_metadata.st_ino == descriptor_metadata.st_ino
    )


def _stable_bytes(descriptor: int, *, byte_limit: int) -> bytes:
    """读取 CAS 文件的受限稳定字节并统一冲突分类。

    参数：``descriptor`` 是原稿、临时或已发布文件；``byte_limit`` 是硬上限。
    返回：完整稳定字节。异常：任何不稳定或超限均抛出
    ``SourcePublicationConflict``。
    """

    try:
        return read_stable_descriptor(
            descriptor,
            byte_limit=byte_limit,
        ).content
    except StableFileAccessError:
        raise SourcePublicationConflict("draft_hash_conflict") from None


def _drain_lease_break_signal() -> bool:
    """同步消费当前线程收到的 Linux 文件租约中断通知。

    参数：无。返回：至少观察到一次租约中断时为 ``True``；非 Linux 路径不调用。
    """

    assert _LEASE_BREAK_SIGNAL is not None
    observed = False
    while True:
        try:
            notification = signal.sigtimedwait({_LEASE_BREAK_SIGNAL}, 0)
        except InterruptedError:
            continue
        if notification is None:
            return observed
        observed = True


def _never_broken() -> bool:
    """为不提供租约中断通知的平台返回恒定未中断状态。

    参数：无。返回：始终为 ``False``；路径与内容身份复核仍独立执行。
    """

    return False


def _sha256(content: bytes) -> str:
    """计算字节内容的稳定 SHA-256。

    参数：``content`` 是完整文件字节。返回：带 ``sha256:`` 前缀的十六进制摘要。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = [
    "NO_EXPECTED_HASH",
    "SourcePublicationConflict",
    "SourcePublicationError",
    "atomic_publish_source",
]
