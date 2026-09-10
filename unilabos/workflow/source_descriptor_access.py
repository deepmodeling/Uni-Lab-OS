"""支持 ``dir_fd`` 平台的工作流源码（Workflow Source）描述符后端。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    binary_open_flags,
    read_stable_descriptor,
)
from unilabos.workflow.source_workspace_errors import SourceWorkspaceError


def directory_flags() -> int:
    """返回安全目录打开标志；参数无，返回禁止链接且要求目录的标志组合。"""

    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def file_flags() -> int:
    """返回安全只读文件标志；参数无，返回禁止链接和 FIFO 阻塞的标志组合。"""

    return binary_open_flags(
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


@contextmanager
def source_parent_descriptor(
    root: Path,
    relative: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
    create: bool,
) -> Iterator[tuple[int, str] | None]:
    """固定注册源码的直接父目录描述符。

    参数：根目录、规范相对路径和预期身份固定来源；``create`` 决定是否创建
    ``workflows``。返回：父目录描述符与文件名或允许缺失；异常映射为稳定工作区
    错误，退出总会关闭描述符。
    """

    root_descriptor = open_directory_chain(root, flags=directory_flags())
    parent_descriptor = -1
    try:
        metadata = os.fstat(root_descriptor)
        if (metadata.st_dev, metadata.st_ino) != expected_root_identity:
            raise SourceWorkspaceError("invalid_input")
        try:
            parent_descriptor = os.open(
                relative.parts[0],
                directory_flags(),
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            if not create:
                yield None
                return
            with suppress(FileExistsError):
                os.mkdir(relative.parts[0], 0o755, dir_fd=root_descriptor)
            parent_descriptor = os.open(
                relative.parts[0],
                directory_flags(),
                dir_fd=root_descriptor,
            )
        yield parent_descriptor, relative.parts[1]
    except SourceWorkspaceError:
        raise
    except OSError:
        raise SourceWorkspaceError("invalid_input") from None
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(root_descriptor)


def open_directory_chain(path: Path, *, flags: int) -> int:
    """逐级禁止链接地打开绝对目录链。

    参数：``path`` 是绝对目录；``flags`` 是目录打开标志。返回：调用者负责关闭
    的最终描述符；任一级失败抛出 ``invalid_package_root``。
    """

    current_descriptor = -1
    try:
        current_descriptor = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=current_descriptor)
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        return current_descriptor
    except (OSError, TypeError, ValueError):
        if current_descriptor >= 0:
            os.close(current_descriptor)
        raise SourceWorkspaceError("invalid_package_root") from None


def open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    missing_ok: bool,
) -> int | None:
    """相对固定父目录打开直接子目录。

    参数：父描述符、单段名称和缺失策略固定操作。返回：子目录描述符或允许的
    ``None``；非法类型、链接或不允许缺失抛出工作区错误。
    """

    try:
        return os.open(name, directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SourceWorkspaceError("invalid_package_root") from None
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_package_root") from None


def read_optional_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    byte_limit: int,
    error_code: str,
) -> bytes | None:
    """相对固定目录读取允许缺失的稳定普通文件。

    参数：父描述符和名称固定文件；``byte_limit`` 是上限；``error_code`` 是失败
    分类。返回：缺失为 ``None``，否则完整字节；不安全时抛出工作区错误。
    """

    descriptor = -1
    try:
        descriptor = os.open(name, file_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError(error_code) from None
    try:
        return read_regular_descriptor(
            descriptor,
            byte_limit=byte_limit,
            error_code=error_code,
        )
    finally:
        os.close(descriptor)


def read_regular_descriptor(
    descriptor: int,
    *,
    byte_limit: int,
    error_code: str,
) -> bytes:
    """读取前后身份一致且不超过上限的普通文件。

    参数：``descriptor`` 是文件；``byte_limit`` 是上限；``error_code`` 是稳定
    分类。返回：完整字节；不稳定、超限或系统故障抛出工作区错误。
    """

    try:
        return read_stable_descriptor(descriptor, byte_limit=byte_limit).content
    except StableFileAccessError:
        raise SourceWorkspaceError(error_code) from None


__all__ = [
    "directory_flags",
    "file_flags",
    "open_child_directory",
    "open_directory_chain",
    "read_optional_regular_at",
    "read_regular_descriptor",
    "source_parent_descriptor",
]
