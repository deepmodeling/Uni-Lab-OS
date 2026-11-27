"""软件包依赖代际的跨进程互斥与双文件原子发布。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from functools import wraps
from pathlib import Path
from typing import Any, BinaryIO, TypeVar, cast

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .lock_codec import declaration_bytes
from .models import (
    DEPENDENCY_DECLARATION_FILE,
    DEPENDENCY_LOCK_FILE,
    DEPENDENCY_MUTATION_GUARD,
    PackageDependencyError,
    PackageDependencyLock,
)

_Operation = TypeVar("_Operation", bound=Callable[..., Any])


def _acquire_dependency_guard(guard_handle: BinaryIO) -> None:
    """跨平台阻塞取得依赖变更锁。"""

    if os.name == "nt":
        guard_handle.seek(0, os.SEEK_END)
        if guard_handle.tell() == 0:
            guard_handle.write(b"\0")
            guard_handle.flush()
        guard_handle.seek(0)
        msvcrt.locking(guard_handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(guard_handle.fileno(), fcntl.LOCK_EX)


def _release_dependency_guard(guard_handle: BinaryIO) -> None:
    """跨平台释放依赖变更锁。"""

    if os.name == "nt":
        guard_handle.seek(0)
        msvcrt.locking(guard_handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(guard_handle.fileno(), fcntl.LOCK_UN)


def serialized_dependency_mutation(operation: _Operation) -> _Operation:
    """让依赖变更在跨进程工作区互斥中执行。

    参数：``operation`` 是依赖管理器上的一次 add、update 或 remove 方法。
    返回：保留原方法元数据、但在文件锁保护内执行的包装方法。
    异常：互斥文件无法打开或底层操作失败时传播原异常；锁总在退出时释放。
    """

    @wraps(operation)
    def serialized(manager: Any, *args: Any, **kwargs: Any) -> Any:
        """持有当前主工作区的唯一依赖变更锁并调用原操作。

        参数：``manager`` 是依赖管理器；``args`` 与 ``kwargs`` 是原方法参数。
        返回：原 add、update 或 remove 方法的完整锁结果。
        异常：文件锁或原方法异常原样传播，且不会遗留进程级互斥。
        """

        # ``guard_path`` 只协调声明和锁的写权威，不进入包目录（PackageCatalog）
        # 或运行时依赖。
        guard_path = manager._workspace.root / DEPENDENCY_MUTATION_GUARD
        try:
            with guard_path.open("a+b") as guard_handle:
                _acquire_dependency_guard(guard_handle)
                try:
                    return operation(manager, *args, **kwargs)
                finally:
                    _release_dependency_guard(guard_handle)
        except OSError as error:
            raise PackageDependencyError("无法取得软件包依赖变更互斥锁") from error

    return cast(_Operation, serialized)


def publish_dependency_state(
    *,
    workspace_root: Path,
    declarations: Mapping[str, tuple[str, str]],
    dependency_lock: PackageDependencyLock,
) -> None:
    """用可回滚的同目录替换发布声明和锁文件。

    参数：``workspace_root`` 是两个权威文件的共同目录；``declarations`` 和
    ``dependency_lock`` 是已完整校验的下一代事实。
    返回：无；两个目标均替换成功后才完成。
    异常：临时文件写入或替换失败时恢复两个旧文件并抛出
    ``PackageDependencyError``；不会留下已知的单文件新代际。
    """

    # ``targets`` 固定同一依赖代际的声明和锁目标及其待发布规范字节。
    targets = (
        (
            workspace_root / DEPENDENCY_DECLARATION_FILE,
            declaration_bytes(declarations),
        ),
        (workspace_root / DEPENDENCY_LOCK_FILE, dependency_lock.to_canonical_bytes()),
    )
    # ``originals`` 保存发布前两个目标的可恢复字节；None 表示目标原本不存在。
    originals = {
        target: target.read_bytes() if target.is_file() else None
        for target, _content in targets
    }
    # ``temporary_paths`` 跟踪尚未替换或需要清理的同目录临时文件。
    temporary_paths: list[Path] = []
    try:
        for target, content in targets:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=workspace_root,
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            # ``temporary_path`` 是与当前目标同目录、可原子替换的候选文件。
            temporary_path = Path(temporary_name)
            temporary_paths.append(temporary_path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for (target, _content), temporary_path in zip(
            targets,
            temporary_paths,
            strict=True,
        ):
            os.replace(temporary_path, target)
    except OSError as error:
        restore_dependency_files(originals)
        raise PackageDependencyError("软件包依赖声明和锁发布失败") from error
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


def restore_dependency_files(originals: Mapping[Path, bytes | None]) -> None:
    """尽最大安全努力恢复一次失败发布前的两个依赖文件。

    参数：``originals`` 是每个目标的旧字节；``None`` 表示目标原本不存在。
    返回：无；每个恢复写使用同目录原子替换。
    异常：恢复本身失败时抛出 ``PackageDependencyError``，要求人工检查工作区；
    不能静默声明事务已恢复。
    """

    try:
        for target, content in originals.items():
            if content is None:
                target.unlink(missing_ok=True)
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.restore.",
                suffix=".tmp",
            )
            # ``temporary_path`` 是恢复单个旧文件的同目录原子替换候选。
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
    except OSError as error:
        raise PackageDependencyError(
            "软件包依赖发布回滚失败，需要人工检查声明和锁"
        ) from error


__all__ = [
    "publish_dependency_state",
    "restore_dependency_files",
    "serialized_dependency_mutation",
]
