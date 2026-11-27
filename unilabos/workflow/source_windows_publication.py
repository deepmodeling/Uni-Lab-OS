"""Windows 工作流草稿（Workflow Draft）发布所需的窄 Win32 适配器。"""

from __future__ import annotations

import ctypes
import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    read_regular_path,
)

FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_CAS_CONFLICT_WINERRORS = frozenset({2, 3, 32, 33})


class WindowsPublicationConflict(RuntimeError):
    """表示 Win32 文件消失、共享冲突或锁冲突。"""


class WindowsPublicationError(RuntimeError):
    """表示 Win32 ACL、空间或 I/O 等基础设施故障。"""


class CtypesApi(Protocol):
    """声明本模块使用的最小 ``ctypes`` 接口。"""

    c_int: Any
    c_uint32: Any
    c_void_p: Any
    c_wchar_p: Any

    def WinDLL(self, name: str, *, use_last_error: bool) -> Any:
        """加载指定的 Win32 动态库。

        参数：``name`` 是 DLL 名；``use_last_error`` 决定是否保存线程错误码。
        返回：可访问 Win32 函数的动态库句柄。异常：加载失败时原样抛出系统错误。
        """

        ...

    def WinError(self, error_number: int) -> OSError:
        """把 Win32 错误码转换成系统异常。

        参数：``error_number`` 是线程最近一次 Win32 错误码。返回：携带
        ``winerror`` 的 ``OSError``；本转换自身不抛出异常。
        """

        ...

    def get_last_error(self) -> int:
        """读取当前线程最近一次 Win32 API 错误码。

        参数：无。返回：非负 Win32 错误码；不改变线程错误状态。
        """

        ...


def replace_windows_file_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
    *,
    ctypes_api: CtypesApi = ctypes,
) -> None:
    """用 ``ReplaceFileW`` 原子替换已有草稿并保留替换瞬间的旧稿。

    参数：``target`` 是规范工作流草稿（Workflow Draft），``replacement`` 是完整
    临时稿，``backup`` 接收替换瞬间的旧字节，``ctypes_api`` 是可测试 Win32
    边界。返回：无。异常：竞争错误抛出 ``WindowsPublicationConflict``；ACL、
    磁盘或 I/O 错误抛出 ``WindowsPublicationError``。
    """

    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes_api.c_wchar_p,
        ctypes_api.c_wchar_p,
        ctypes_api.c_wchar_p,
        ctypes_api.c_uint32,
        ctypes_api.c_void_p,
        ctypes_api.c_void_p,
    ]
    replace_file.restype = ctypes_api.c_int
    succeeded = replace_file(
        _extended_path(target),
        _extended_path(replacement),
        _extended_path(backup),
        0,
        None,
        None,
    )
    if not succeeded:
        _raise_last_error(ctypes_api)


@contextmanager
def hold_windows_directory_chain(
    paths: Sequence[Path],
    *,
    ctypes_api: CtypesApi = ctypes,
) -> Iterator[None]:
    """固定从卷根到草稿父目录的目录链，阻止发布窗口内重命名。

    参数：``paths`` 是按祖先到子目录排序的绝对路径；``ctypes_api`` 提供原生
    API。返回：上下文不产出值。任一目录无法固定时失败关闭；退出时逆序释放句柄。
    """

    handles: list[int] = []
    try:
        for path in paths:
            handles.append(_open_directory(path, ctypes_api=ctypes_api))
        yield
    finally:
        for handle in reversed(handles):
            with suppress(Exception):
                _close_directory(handle, ctypes_api=ctypes_api)


def verify_windows_backup_or_restore(
    *,
    parent: Path,
    target: Path,
    backup: Path,
    expected_hash: str,
    replacement_hash: str,
    byte_limit: int,
) -> None:
    """核验替换瞬间旧稿；若竞争则恢复并保留无法归属的外部字节。

    参数：``parent`` 和两个路径限定同一次工作流草稿（Workflow Draft）发布；
    ``expected_hash`` 是原稿条件，``replacement_hash`` 识别本次写入，字节上限
    约束恢复证据。返回：backup 匹配时删除它；竞争恢复后抛出冲突；恢复不可证明
    时抛出 ``WindowsPublicationError``。
    """

    try:
        backup_snapshot = read_regular_path(
            backup,
            byte_limit=byte_limit,
            missing_ok=False,
        )
    except StableFileAccessError:
        backup_snapshot = None
    if (
        backup_snapshot is not None
        and _sha256(backup_snapshot.content) == expected_hash
    ):
        backup.unlink()
        return
    if not _restore_competitor(
        parent=parent,
        target=target,
        backup=backup,
        replacement_hash=replacement_hash,
        byte_limit=byte_limit,
    ):
        raise WindowsPublicationError("publication_failed")
    raise WindowsPublicationConflict("draft_hash_conflict")


def restore_missing_windows_target(*, target: Path, backup: Path) -> bool:
    """恢复原生替换部分失败留下的规范路径缺口。

    参数：``target`` 是应存在的规范工作流草稿（Workflow Draft），``backup`` 是
    Win32 可能留下的旧稿。返回：目标已经存在或成功恢复时为 ``True``；没有可证明
    的旧稿或恢复失败时为 ``False``。
    """

    if target.exists():
        return True
    if not backup.exists():
        return False
    try:
        backup.rename(target)
    except OSError:
        return False
    return True


def windows_directory_chain(parent: Path) -> tuple[Path, ...]:
    """建立从卷根到工作流草稿（Workflow Draft）父目录的路径链。

    参数：``parent`` 是规范草稿父目录。返回：按祖先到子目录排列的绝对路径；
    路径规范化错误原样抛出，调用者不得在不完整目录链上继续发布。
    """

    absolute = Path(os.path.abspath(parent))
    parts = absolute.parts
    current = Path(parts[0])
    paths = [current]
    for part in parts[1:]:
        current /= part
        paths.append(current)
    return tuple(paths)


def _extended_path(path: Path) -> str:
    """把普通路径转换成 Win32 Unicode 长路径。

    参数：``path`` 是文件或目录路径。返回：带 ``\\?\\`` 或 UNC 长路径前缀的
    绝对字符串；路径解析错误原样抛出。
    """

    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return f"\\\\?\\UNC\\{absolute[2:]}"
    return f"\\\\?\\{absolute}"


def _restore_competitor(
    *,
    parent: Path,
    target: Path,
    backup: Path,
    replacement_hash: str,
    byte_limit: int,
) -> bool:
    """恢复替换瞬间的竞争稿，并避免丢失更晚的外部写入。

    参数：``target`` 当前通常是本次替换稿，``backup`` 是竞争稿，哈希用于识别
    本次写入。返回：所有外部字节均保留且规范路径可证明时为 ``True``。
    """

    displaced = parent / f".{target.name}.{uuid4().hex}.displaced"
    try:
        replace_windows_file_with_backup(target, backup, displaced)
        displaced_snapshot = read_regular_path(
            displaced,
            byte_limit=byte_limit,
            missing_ok=False,
        )
        if (
            displaced_snapshot is not None
            and _sha256(displaced_snapshot.content) == replacement_hash
        ):
            displaced.unlink()
            return True
        # ``displaced`` 若不是本次写入，就是恢复窗口内更晚的外部版本；把它放回
        # 规范路径，同时把较早竞争稿保存在 artifact 中，禁止丢失任一版本。
        preserved = parent / f".{target.name}.{uuid4().hex}.cas"
        replace_windows_file_with_backup(target, displaced, preserved)
        return True
    except (
        OSError,
        StableFileAccessError,
        WindowsPublicationConflict,
        WindowsPublicationError,
    ):
        return False


def _sha256(content: bytes) -> str:
    """计算 Windows CAS 证据使用的稳定内容身份。

    参数：``content`` 是完整文件字节。返回：带算法前缀的 SHA-256 哈希；无异常。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _raise_last_error(ctypes_api: CtypesApi) -> None:
    """读取最近 WinError，并稳定分类为竞争或基础设施故障。

    参数：``ctypes_api`` 提供当前线程错误码和异常转换。返回：永不返回。
    异常：文件消失、共享或锁冲突抛出 ``WindowsPublicationConflict``；其他错误
    抛出 ``WindowsPublicationError``。
    """

    error_number = ctypes_api.get_last_error()
    system_error = ctypes_api.WinError(error_number)
    if error_number in _CAS_CONFLICT_WINERRORS:
        raise WindowsPublicationConflict from system_error
    raise WindowsPublicationError from system_error


def _open_directory(path: Path, *, ctypes_api: CtypesApi) -> int:
    """打开不共享 delete/rename 的目录句柄。

    参数：``path`` 是目录链成员，``ctypes_api`` 提供 Win32 API。返回：原生句柄
    整数；打开失败按最近 WinError 分类。
    """

    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes_api.c_wchar_p,
        ctypes_api.c_uint32,
        ctypes_api.c_uint32,
        ctypes_api.c_void_p,
        ctypes_api.c_uint32,
        ctypes_api.c_uint32,
        ctypes_api.c_void_p,
    ]
    create_file.restype = ctypes_api.c_void_p
    handle = create_file(
        _extended_path(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    raw_handle = getattr(handle, "value", handle)
    invalid_handle = ctypes_api.c_void_p(-1).value
    handle_value = int(raw_handle) if raw_handle is not None else invalid_handle
    if handle_value == invalid_handle:
        _raise_last_error(ctypes_api)
    return handle_value


def _close_directory(handle: int, *, ctypes_api: CtypesApi) -> None:
    """关闭一个 Windows 目录链句柄。

    参数：``handle`` 是 ``CreateFileW`` 返回的目录句柄；``ctypes_api`` 提供
    ``CloseHandle``。返回：无；原生关闭结果不覆盖调用者已有的主要发布结论。
    """

    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes_api.c_void_p]
    close_handle.restype = ctypes_api.c_int
    close_handle(handle)


__all__ = [
    "WindowsPublicationConflict",
    "WindowsPublicationError",
    "hold_windows_directory_chain",
    "replace_windows_file_with_backup",
    "restore_missing_windows_target",
    "verify_windows_backup_or_restore",
    "windows_directory_chain",
]
