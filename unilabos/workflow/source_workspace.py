"""工作流源码（Workflow Source）的受限本地文件访问实现。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from unilabos.workflow.source_descriptor_access import (
    directory_flags as _directory_flags,
)
from unilabos.workflow.source_descriptor_access import (
    file_flags as _file_flags,
)
from unilabos.workflow.source_descriptor_access import (
    open_child_directory as _open_child_directory,
)
from unilabos.workflow.source_descriptor_access import (
    open_directory_chain as _open_directory_chain,
)
from unilabos.workflow.source_descriptor_access import (
    read_optional_regular_at as _read_optional_regular_at,
)
from unilabos.workflow.source_descriptor_access import (
    read_regular_descriptor as _read_regular_descriptor,
)
from unilabos.workflow.source_descriptor_access import (
    source_parent_descriptor as _source_parent_descriptor,
)
from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    is_reparse_point,
    read_stable_descriptor,
)
from unilabos.workflow.source_path_access import (
    assert_package_root as assert_package_root_by_path,
)
from unilabos.workflow.source_path_access import (
    publish_registered_source as publish_registered_source_by_path,
)
from unilabos.workflow.source_path_access import (
    read_package_manifest as read_package_manifest_by_path,
)
from unilabos.workflow.source_path_access import (
    read_registered_source as read_registered_source_by_path,
)
from unilabos.workflow.source_path_access import (
    registered_source_signature as registered_source_signature_by_path,
)
from unilabos.workflow.source_path_access import (
    validate_declared_sources as validate_declared_sources_by_path,
)
from unilabos.workflow.source_path_access import (
    validate_registered_source as validate_registered_source_by_path,
)
from unilabos.workflow.source_publication import (
    NO_EXPECTED_HASH,
    SourcePublicationConflict,
    SourcePublicationError,
    atomic_publish_source,
)
from unilabos.workflow.source_workspace_errors import (
    SourceWorkspaceConflict,
    SourceWorkspaceError,
)

MANIFEST_BYTE_LIMIT = 1024 * 1024
WORKFLOW_SOURCE_BYTE_LIMIT = 8 * 1024 * 1024
_DIRECTORY_FD_PATHS_SUPPORTED = all(
    operation in os.supports_dir_fd
    for operation in (os.open, os.stat, os.mkdir, os.unlink)
)


@dataclass(frozen=True)
class PackageRootSnapshot:
    """一次显式授权目录读取时固定的目录身份。"""

    selected_root: Path
    identity: tuple[int, int]
    manifest_bytes: bytes


@dataclass(frozen=True)
class PackageSourceSnapshot:
    """经普通文件与 UTF-8 校验后的实际 Python 包目录身份。"""

    package_root: Path
    identity: tuple[int, int]


@dataclass(frozen=True)
class SourceDocument:
    """安全读取后的工作流源码（Workflow Source）内容与版本事实。"""

    python_source: str
    draft_hash: str
    update_time: str


@dataclass(frozen=True)
class PinnedPackageRoots:
    """固定到文件描述符的可编辑包（Editable Package）目录集合。"""

    _entries: tuple[tuple[Path, tuple[int, int], int | None], ...]

    def assert_current(self) -> None:
        """确认规范路径仍指向发现时固定的全部包目录。

        参数：无。
        返回：无；全部路径身份与固定文件描述符一致时正常返回。
        异常：目录被替换、变成符号链接或不可访问时抛出
        ``SourceWorkspaceError``，供数据库事务在提交前失败关闭。
        """

        for package_root, expected_identity, pinned_descriptor in self._entries:
            # ``expected_identity`` 来自发现计划；``pinned_descriptor`` 保持原目录
            # 存活，二者共同防止路径在注册事务期间被静默替换。
            if pinned_descriptor is None:
                try:
                    assert_package_root_by_path(package_root, expected_identity)
                except StableFileAccessError:
                    raise SourceWorkspaceError("invalid_package_root") from None
                continue
            pinned_metadata = os.fstat(pinned_descriptor)
            if (
                pinned_metadata.st_dev,
                pinned_metadata.st_ino,
            ) != expected_identity:
                raise SourceWorkspaceError("invalid_package_root")
            current_descriptor = _open_directory_chain(
                package_root,
                flags=_directory_flags(),
            )
            try:
                current_metadata = os.fstat(current_descriptor)
                if (
                    current_metadata.st_dev,
                    current_metadata.st_ino,
                ) != expected_identity:
                    raise SourceWorkspaceError("invalid_package_root")
            finally:
                os.close(current_descriptor)


@contextmanager
def pin_package_roots(
    root_identities: Iterable[tuple[Path, tuple[int, int]]],
) -> Iterator[PinnedPackageRoots]:
    """把发现计划中的包目录身份固定到注册事务结束。

    参数：``root_identities`` 逐项给出规范包路径和发现时的设备/索引节点身份。
    返回：上下文内提供可在事务提交前复核的 ``PinnedPackageRoots``。
    异常：任一目录身份已经变化或无法安全打开时抛出
    ``SourceWorkspaceError``；退出上下文总会关闭全部文件描述符。
    """

    pinned_entries: list[tuple[Path, tuple[int, int], int | None]] = []
    try:
        for package_root, expected_identity in tuple(root_identities):
            # ``package_path`` 保留发现计划的绝对规范路径，不重新解释包身份。
            package_path = Path(package_root)
            if not _DIRECTORY_FD_PATHS_SUPPORTED:
                try:
                    assert_package_root_by_path(package_path, expected_identity)
                except StableFileAccessError:
                    raise SourceWorkspaceError("invalid_package_root") from None
                pinned_entries.append((package_path, expected_identity, None))
                continue
            descriptor = _open_directory_chain(
                package_path,
                flags=_directory_flags(),
            )
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != expected_identity:
                os.close(descriptor)
                raise SourceWorkspaceError("invalid_package_root")
            pinned_entries.append((package_path, expected_identity, descriptor))
        pinned_roots = PinnedPackageRoots(tuple(pinned_entries))
        pinned_roots.assert_current()
        yield pinned_roots
    finally:
        for _package_root, _expected_identity, descriptor in pinned_entries:
            if descriptor is not None:
                os.close(descriptor)


def validate_source_registration(
    *,
    package_root: str | Path,
    relative_path: str,
) -> tuple[Path, str]:
    """验证单项来源注册指向受限的 ``workflows/*.py`` 路径。

    参数：``package_root`` 是实际 Python 包目录；``relative_path`` 是包内源码路径。
    返回：规范绝对包目录和规范 POSIX 相对路径。
    异常：目录、路径、符号链接或既有目标不安全时抛出 ``SourceWorkspaceError``。
    """

    registration = {
        "package_root": str(package_root),
        "relative_path": relative_path,
    }
    root, normalized_relative, root_identity = _source_location(registration)
    if not _DIRECTORY_FD_PATHS_SUPPORTED:
        try:
            validate_registered_source_by_path(
                root,
                normalized_relative,
                expected_root_identity=root_identity,
            )
        except StableFileAccessError:
            raise SourceWorkspaceError("invalid_input") from None
        return root, normalized_relative.as_posix()
    with _source_parent_descriptor(
        root,
        normalized_relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is not None:
            parent_descriptor, filename = source_parent
            try:
                metadata = os.stat(
                    filename,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError:
                raise SourceWorkspaceError("invalid_input") from None
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise SourceWorkspaceError("invalid_input")
    return root, normalized_relative.as_posix()


def read_registered_source(
    registration: Mapping[str, Any],
) -> SourceDocument | None:
    """读取一项已注册工作流源码并执行普通文件、UTF-8 与大小校验。

    参数：``registration`` 含持久化 ``package_root`` 与 ``relative_path``。
    返回：源码缺失时为 ``None``，否则返回内容、哈希和修改时间。
    异常：路径不安全、非普通文件、超限或非 UTF-8 时抛出
    ``SourceWorkspaceError``。
    """

    root, relative, root_identity = _source_location(registration)
    if not _DIRECTORY_FD_PATHS_SUPPORTED:
        try:
            snapshot = read_registered_source_by_path(
                root,
                relative,
                expected_root_identity=root_identity,
                byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
            )
        except StableFileAccessError:
            raise SourceWorkspaceError("invalid_input") from None
        if snapshot is None:
            return None
        return _source_document(snapshot.content, snapshot.metadata.st_mtime)
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is None:
            return None
        parent_descriptor, filename = source_parent
        descriptor = -1
        try:
            descriptor = os.open(filename, _file_flags(), dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        except OSError:
            raise SourceWorkspaceError("invalid_input") from None
        try:
            snapshot = read_stable_descriptor(
                descriptor,
                byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
            )
            metadata = snapshot.metadata
            raw_source = snapshot.content
        except StableFileAccessError:
            raise SourceWorkspaceError("invalid_input") from None
        finally:
            os.close(descriptor)
    return _source_document(raw_source, metadata.st_mtime)


def registered_source_signature(
    registration: Mapping[str, Any],
) -> tuple[Any, ...]:
    """返回无需读取内容的安全文件签名，供源码监视器（Source Monitor）去抖。

    参数：``registration`` 是已持久化来源身份。
    返回：缺失标记，或普通文件的设备、索引节点、大小和时间签名。
    异常：路径不安全或目标不是普通文件时抛出 ``SourceWorkspaceError``。
    """

    root, relative, root_identity = _source_location(registration)
    if not _DIRECTORY_FD_PATHS_SUPPORTED:
        try:
            return registered_source_signature_by_path(
                root,
                relative,
                expected_root_identity=root_identity,
            )
        except StableFileAccessError:
            raise SourceWorkspaceError("invalid_input") from None
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is None:
            return ("missing",)
        parent_descriptor, filename = source_parent
        try:
            metadata = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return ("missing",)
        except OSError:
            raise SourceWorkspaceError("invalid_input") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceWorkspaceError("invalid_input")
    return (
        "file",
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def write_registered_source(
    registration: Mapping[str, Any],
    content: bytes,
    *,
    expected_hash: object | str | None = NO_EXPECTED_HASH,
) -> None:
    """在注册来源的规范路径执行受限原子 CAS 写入。

    参数：``registration`` 是来源身份；``content`` 是 UTF-8 源码字节；
    ``expected_hash`` 是可选的当前草稿哈希条件。
    返回：无；成功时规范路径完整替换且目录已同步。
    异常：内容超限或路径不安全抛出 ``SourceWorkspaceError``；CAS 冲突抛出
    ``SourceWorkspaceConflict``。
    """

    if len(content) > WORKFLOW_SOURCE_BYTE_LIMIT:
        raise SourceWorkspaceError("invalid_input")
    root, relative, root_identity = _source_location(registration)
    if not _DIRECTORY_FD_PATHS_SUPPORTED:
        try:
            publish_registered_source_by_path(
                root,
                relative,
                content,
                expected_root_identity=root_identity,
                byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
            return
        except SourcePublicationConflict:
            raise SourceWorkspaceConflict("draft_hash_conflict") from None
        except SourcePublicationError:
            raise SourceWorkspaceError("internal_error") from None
        except StableFileAccessError:
            raise SourceWorkspaceError("invalid_input") from None
    # 首次保存允许创建固定的 workflows 目录，但不创建 manifest 声明本身。
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=True,
    ):
        pass
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is None:
            raise SourceWorkspaceError("invalid_input")
        parent_descriptor, filename = source_parent
        try:
            atomic_publish_source(
                parent_descriptor=parent_descriptor,
                target_name=filename,
                content=content,
                byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
        except SourcePublicationConflict:
            raise SourceWorkspaceConflict("draft_hash_conflict") from None
        except SourcePublicationError:
            raise SourceWorkspaceError("internal_error") from None


def read_package_root(selected_root: str | Path) -> PackageRootSnapshot:
    """安全读取显式授权目录中的 ``package.yaml``。

    参数：``selected_root`` 是启动配置明确列出的包选择目录。
    返回：目录路径、设备/索引节点身份和受限大小的 manifest 字节。
    异常：目录、符号链接或 manifest 不安全时抛出 ``SourceWorkspaceError``。
    """

    root = Path(os.path.abspath(selected_root))
    try:
        root_metadata = root.lstat()
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_package_root") from None
    if not stat.S_ISDIR(root_metadata.st_mode) or _contains_symlink(root):
        raise SourceWorkspaceError("invalid_package_root")

    if not _DIRECTORY_FD_PATHS_SUPPORTED:
        try:
            root_identity, manifest_bytes = read_package_manifest_by_path(
                root,
                byte_limit=MANIFEST_BYTE_LIMIT,
            )
        except StableFileAccessError:
            raise SourceWorkspaceError("invalid_manifest") from None
        return PackageRootSnapshot(
            selected_root=root,
            identity=root_identity,
            manifest_bytes=manifest_bytes,
        )

    directory_flags = _directory_flags()
    file_flags = _file_flags()
    root_descriptor = -1
    manifest_descriptor = -1
    try:
        root_descriptor = _open_directory_chain(root, flags=directory_flags)
        opened_metadata = os.fstat(root_descriptor)
        if (opened_metadata.st_dev, opened_metadata.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            raise SourceWorkspaceError("invalid_package_root")
        manifest_descriptor = os.open(
            "package.yaml",
            file_flags,
            dir_fd=root_descriptor,
        )
        manifest_bytes = _read_regular_descriptor(
            manifest_descriptor,
            byte_limit=MANIFEST_BYTE_LIMIT,
            error_code="invalid_manifest",
        )
    except SourceWorkspaceError:
        raise
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_manifest") from None
    finally:
        if manifest_descriptor >= 0:
            os.close(manifest_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)
    return PackageRootSnapshot(
        selected_root=root,
        identity=(root_metadata.st_dev, root_metadata.st_ino),
        manifest_bytes=manifest_bytes,
    )


def validate_declared_sources(
    root_snapshot: PackageRootSnapshot,
    *,
    package_id: str,
    relative_paths: Iterable[str],
) -> PackageSourceSnapshot:
    """校验 manifest 指向的实际 Python 包目录和已有源码。

    参数：``root_snapshot`` 是 manifest 读取时固定的授权目录身份；
    ``package_id`` 是已验证的包目录名；``relative_paths`` 是规范
    ``workflows/*.py`` 路径集合。
    返回：实际 Python 包目录路径及其设备/索引节点身份。
    异常：目录身份变化、符号链接、非普通文件、超限或非 UTF-8 时抛出
    ``SourceWorkspaceError``；缺失源码保持合法且不会被创建。
    """

    if not _DIRECTORY_FD_PATHS_SUPPORTED:
        try:
            package_root, package_identity = validate_declared_sources_by_path(
                root_snapshot.selected_root,
                expected_selected_identity=root_snapshot.identity,
                package_id=package_id,
                relative_paths=relative_paths,
                source_byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
            )
        except StableFileAccessError as error:
            code = (
                "invalid_workflow_source"
                if str(error) == "invalid_utf8_source"
                else "invalid_package_root"
            )
            raise SourceWorkspaceError(code) from None
        return PackageSourceSnapshot(
            package_root=package_root,
            identity=package_identity,
        )

    selected_descriptor = -1
    package_descriptor = -1
    workflows_descriptor = -1
    try:
        selected_descriptor = _open_directory_chain(
            root_snapshot.selected_root,
            flags=_directory_flags(),
        )
        selected_metadata = os.fstat(selected_descriptor)
        if (
            selected_metadata.st_dev,
            selected_metadata.st_ino,
        ) != root_snapshot.identity:
            raise SourceWorkspaceError("invalid_package_root")
        package_descriptor = _open_child_directory(
            selected_descriptor,
            package_id,
            missing_ok=False,
        )
        assert package_descriptor is not None
        package_metadata = os.fstat(package_descriptor)
        workflows_descriptor = _open_child_directory(
            package_descriptor,
            "workflows",
            missing_ok=True,
        )
        if workflows_descriptor is not None:
            for relative_path in tuple(relative_paths):
                # 路径结构已由 manifest 模块验证；这里只用最终文件名做 dir_fd 读取。
                filename = PurePosixPath(relative_path).name
                source_bytes = _read_optional_regular_at(
                    workflows_descriptor,
                    filename,
                    byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
                    error_code="invalid_workflow_source",
                )
                if source_bytes is not None:
                    try:
                        source_bytes.decode("utf-8")
                    except UnicodeError:
                        raise SourceWorkspaceError("invalid_workflow_source") from None
    except SourceWorkspaceError:
        raise
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_package_root") from None
    finally:
        if workflows_descriptor is not None and workflows_descriptor >= 0:
            os.close(workflows_descriptor)
        if package_descriptor >= 0:
            os.close(package_descriptor)
        if selected_descriptor >= 0:
            os.close(selected_descriptor)
    return PackageSourceSnapshot(
        package_root=root_snapshot.selected_root / package_id,
        identity=(package_metadata.st_dev, package_metadata.st_ino),
    )


def _contains_symlink(path: Path) -> bool:
    """判断绝对路径链中是否含符号链接或 Windows 重解析点。

    参数：``path`` 是待核验的显式授权目录。
    返回：任一祖先或目录本身是符号链接时为 ``True``。
    """

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or is_reparse_point(metadata):
            return True
    return False


def _source_location(
    registration: Mapping[str, Any],
) -> tuple[Path, PurePosixPath, tuple[int, int]]:
    """规范并验证一项持久来源注册的目录与相对路径。

    参数：``registration`` 必须含 ``package_root`` 和 ``relative_path``。
    返回：无符号链接的绝对包目录、规范 ``workflows/*.py`` 路径和目录身份。
    异常：字段、目录或路径不合法时抛出 ``SourceWorkspaceError``。
    """

    try:
        stored_root = Path(registration["package_root"])
        raw_relative = registration["relative_path"]
    except (KeyError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_input") from None
    if (
        not isinstance(raw_relative, str)
        or "\\" in raw_relative
        or "\x00" in raw_relative
    ):
        raise SourceWorkspaceError("invalid_input")
    absolute_root = Path(os.path.abspath(stored_root))
    try:
        root_metadata = absolute_root.lstat()
    except OSError:
        raise SourceWorkspaceError("invalid_input") from None
    if not stat.S_ISDIR(root_metadata.st_mode) or _contains_symlink(absolute_root):
        raise SourceWorkspaceError("invalid_input")
    try:
        root = absolute_root.resolve(strict=True)
    except OSError:
        raise SourceWorkspaceError("invalid_input") from None
    if not root.is_dir():
        raise SourceWorkspaceError("invalid_input")
    relative = PurePosixPath(raw_relative)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "workflows"
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix != ".py"
        or not relative.stem
    ):
        raise SourceWorkspaceError("invalid_input")
    return root, relative, (root_metadata.st_dev, root_metadata.st_ino)


def _source_document(raw_source: bytes, modified_at: float) -> SourceDocument:
    """把稳定字节快照转换为公共工作流源码文档。

    参数：``raw_source`` 是完整源码字节；``modified_at`` 是同一稳定快照的修改
    时间。返回：UTF-8 源码、哈希和 RFC3339 时间。异常：非法 UTF-8 映射为
    ``SourceWorkspaceError``。
    """

    try:
        python_source = raw_source.decode("utf-8")
    except UnicodeError:
        raise SourceWorkspaceError("invalid_input") from None
    return SourceDocument(
        python_source=python_source,
        draft_hash=_sha256(raw_source),
        update_time=_mtime_rfc3339(modified_at),
    )


def _sha256(content: bytes) -> str:
    """计算工作流源码字节的稳定 SHA-256。

    参数：``content`` 是完整源码字节。
    返回：带 ``sha256:`` 前缀的小写十六进制摘要。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _mtime_rfc3339(value: float) -> str:
    """把文件系统修改时间转换成 UTC RFC3339 文本。

    参数：``value`` 是文件系统返回的 Unix 秒时间。
    返回：以 ``Z`` 结尾的 UTC 时间文本。
    """

    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "MANIFEST_BYTE_LIMIT",
    "NO_EXPECTED_HASH",
    "WORKFLOW_SOURCE_BYTE_LIMIT",
    "PackageRootSnapshot",
    "PackageSourceSnapshot",
    "PinnedPackageRoots",
    "SourceDocument",
    "SourceWorkspaceConflict",
    "SourceWorkspaceError",
    "pin_package_roots",
    "read_package_root",
    "read_registered_source",
    "registered_source_signature",
    "validate_declared_sources",
    "validate_source_registration",
    "write_registered_source",
]
