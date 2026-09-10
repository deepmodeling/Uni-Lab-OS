"""工作流源码发布（Source Publication）的系统错误分类深模块。"""

from __future__ import annotations

import errno
from typing import NoReturn


class SourcePublicationConflict(RuntimeError):
    """表示规范源码在 CAS 发布期间被其他写入者改变。"""


class SourcePublicationError(RuntimeError):
    """表示原子发布无法安全完成的基础设施错误。"""


# ``_CONFLICT_ERRNOS`` 只包含普通文件操作中能够表达文件世代、目录项或系统
# 竞争的 POSIX 错误；权限、空间、I/O 和描述符耗尽必须保留为基础设施故障。
_CONFLICT_ERRNOS = frozenset(
    error_number
    for error_number in (
        errno.ENOENT,
        errno.EEXIST,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "ESTALE", None),
        getattr(errno, "EBUSY", None),
    )
    if error_number is not None
)

# ``_CONFLICT_WINERRORS`` 与 Win32 文件缺失、共享冲突和锁冲突保持一致；其他
# Windows 错误（包括 access denied）是基础设施故障，不得伪装成内容冲突。
_CONFLICT_WINERRORS = frozenset({2, 3, 32, 33})

# ``_LOCK_CONTENTION_ERRNOS`` 只用于非阻塞锁 API。同一个 EACCES 在 open/replace
# 表示权限故障，但 POSIX/Windows CRT 非阻塞锁用它表达资源已由其他进程占用。
_LOCK_CONTENTION_ERRNOS = frozenset(
    error_number
    for error_number in (
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if error_number is not None
)
_LOCK_CONTENTION_WINERRORS = frozenset({32, 33})


def raise_classified_publication_os_error(error: OSError) -> NoReturn:
    """把系统错误稳定映射为源码发布冲突或基础设施故障。

    参数：``error`` 是文件打开、替换或同步产生的原始系统异常；其
    ``winerror`` 在 Windows 上优先于 POSIX ``errno``。返回：永不返回。
    异常：只在错误明确表示文件世代或锁竞争时抛出
    ``SourcePublicationConflict``；其余错误抛出 ``SourcePublicationError``。
    """

    winerror = getattr(error, "winerror", None)
    if isinstance(winerror, int):
        if winerror in _CONFLICT_WINERRORS:
            raise SourcePublicationConflict("draft_hash_conflict") from error
        raise SourcePublicationError("publication_failed") from error
    if error.errno in _CONFLICT_ERRNOS:
        raise SourcePublicationConflict("draft_hash_conflict") from error
    raise SourcePublicationError("publication_failed") from error


def raise_classified_lock_os_error(error: OSError) -> NoReturn:
    """按非阻塞锁 API 的 errno/winerror 语义分类系统错误。

    参数：``error`` 是 POSIX ``flock`` 或 Windows CRT ``locking`` 获取非阻塞
    锁时产生的原始系统异常；Windows ``winerror`` 优先于 POSIX ``errno``。
    返回：永不返回。异常：EACCES、EAGAIN/EWOULDBLOCK、EDEADLOCK 以及 Windows
    共享/锁冲突抛出 ``SourcePublicationConflict``；其他权限、I/O 或编程错误
    抛出 ``SourcePublicationError``。
    """

    winerror = getattr(error, "winerror", None)
    if isinstance(winerror, int):
        if winerror in _LOCK_CONTENTION_WINERRORS:
            raise SourcePublicationConflict("draft_hash_conflict") from error
        raise SourcePublicationError("publication_failed") from error
    if error.errno in _LOCK_CONTENTION_ERRNOS:
        raise SourcePublicationConflict("draft_hash_conflict") from error
    raise SourcePublicationError("publication_failed") from error


__all__ = [
    "SourcePublicationConflict",
    "SourcePublicationError",
    "raise_classified_lock_os_error",
    "raise_classified_publication_os_error",
]
