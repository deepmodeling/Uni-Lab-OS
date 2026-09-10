"""无 ``dir_fd`` 平台的工作流源码（Workflow Source）绝对路径后端。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    StableFileSnapshot,
    assert_directory_identity,
    directory_identity,
    ensure_child_directory,
    read_regular_path,
    regular_path_signature,
)
from unilabos.workflow.source_publication import atomic_publish_source


def assert_package_root(
    package_root: Path,
    expected_identity: tuple[int, int],
) -> None:
    """复核无 ``dir_fd`` 包目录仍是授权时的同一物理目录。

    参数：``package_root`` 是规范绝对包路径；``expected_identity`` 是设备/索引
    节点身份。返回：身份一致时无返回值；不安全或变化时抛出
    ``StableFileAccessError``。
    """

    assert_directory_identity(package_root, expected_identity)


def validate_registered_source(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
) -> None:
    """验证允许缺失的既有注册目标是安全普通文件。

    参数：包路径、规范相对路径和预期根身份共同固定来源。返回：安全或缺失时无
    返回值；目录/目标竞态、符号链接或类型错误抛出 ``StableFileAccessError``。
    """

    regular_path_signature(
        package_root / relative_path.as_posix(),
        missing_ok=True,
    )
    assert_directory_identity(package_root, expected_root_identity)


def read_registered_source(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
    byte_limit: int,
) -> StableFileSnapshot | None:
    """读取注册来源的稳定普通文件快照。

    参数：包路径、相对路径和根身份固定来源；``byte_limit`` 是源码硬上限。返回：
    缺失时为 ``None``，否则为稳定字节及元数据；不安全时抛出
    ``StableFileAccessError``。
    """

    snapshot = read_regular_path(
        package_root / relative_path.as_posix(),
        byte_limit=byte_limit,
        missing_ok=True,
    )
    assert_directory_identity(package_root, expected_root_identity)
    return snapshot


def registered_source_signature(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
) -> tuple[object, ...]:
    """读取注册来源的稳定文件世代签名。

    参数：包路径、相对路径和预期根身份固定来源。返回：缺失或普通文件签名；
    不安全和身份变化时抛出 ``StableFileAccessError``。
    """

    signature = regular_path_signature(
        package_root / relative_path.as_posix(),
        missing_ok=True,
    )
    assert_directory_identity(package_root, expected_root_identity)
    return signature


def publish_registered_source(
    package_root: Path,
    relative_path: PurePosixPath,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int],
    byte_limit: int,
    expected_hash: object | str | None,
) -> None:
    """创建固定父目录并通过绝对路径后端原子发布注册源码。

    参数：包路径、相对路径、内容和根身份固定写入对象；``byte_limit`` 限制内容；
    ``expected_hash`` 是 CAS 条件。返回：发布并复核根身份后无返回值；安全、CAS
    和基础设施错误沿用下层分类。
    """

    source_parent = ensure_child_directory(
        package_root,
        expected_root_identity=expected_root_identity,
        child_name=relative_path.parts[0],
    )
    atomic_publish_source(
        parent_path=source_parent,
        target_name=relative_path.parts[1],
        content=content,
        byte_limit=byte_limit,
        expected_hash=expected_hash,
    )
    assert_directory_identity(package_root, expected_root_identity)


def read_package_manifest(
    selected_root: Path,
    *,
    byte_limit: int,
) -> tuple[tuple[int, int], bytes]:
    """读取显式选择目录中的稳定 ``package.yaml``。

    参数：``selected_root`` 是无符号链接绝对目录；``byte_limit`` 是 manifest
    上限。返回：目录身份与完整 manifest 字节；目录或文件不安全时抛出
    ``StableFileAccessError``。
    """

    root_identity = directory_identity(selected_root)
    snapshot = read_regular_path(
        selected_root / "package.yaml",
        byte_limit=byte_limit,
        missing_ok=False,
    )
    assert snapshot is not None
    assert_directory_identity(selected_root, root_identity)
    return root_identity, snapshot.content


def validate_declared_sources(
    selected_root: Path,
    *,
    expected_selected_identity: tuple[int, int],
    package_id: str,
    relative_paths: Iterable[str],
    source_byte_limit: int,
) -> tuple[Path, tuple[int, int]]:
    """验证 manifest 声明的包目录与允许缺失的 Python 源码。

    参数：选择目录及身份固定授权根；``package_id`` 是直接子包；相对路径已经由
    manifest 语法层验证；``source_byte_limit`` 是源码上限。返回：实际包路径和
    身份。异常：目录竞态、符号链接、非法文件、超限或非 UTF-8 抛出
    ``StableFileAccessError``。
    """

    assert_directory_identity(selected_root, expected_selected_identity)
    package_root = selected_root / package_id
    package_identity = directory_identity(package_root)
    workflows_root = package_root / "workflows"
    try:
        workflows_identity = directory_identity(workflows_root)
    except StableFileAccessError:
        if workflows_root.exists() or workflows_root.is_symlink():
            raise
        workflows_identity = None
    if workflows_identity is not None:
        for relative_path in tuple(relative_paths):
            filename = PurePosixPath(relative_path).name
            snapshot = read_regular_path(
                workflows_root / filename,
                byte_limit=source_byte_limit,
                missing_ok=True,
            )
            if snapshot is not None:
                try:
                    snapshot.content.decode("utf-8")
                except UnicodeError:
                    raise StableFileAccessError("invalid_utf8_source") from None
        assert_directory_identity(workflows_root, workflows_identity)
    assert_directory_identity(package_root, package_identity)
    assert_directory_identity(selected_root, expected_selected_identity)
    return package_root, package_identity


__all__ = [
    "assert_package_root",
    "publish_registered_source",
    "read_package_manifest",
    "read_registered_source",
    "registered_source_signature",
    "validate_declared_sources",
    "validate_registered_source",
]
