"""工作流源码（Workflow Source）的稳定文件读取与绝对路径安全回退。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


class StableFileAccessError(RuntimeError):
    """表示无法证明一次文件或目录观察保持稳定。"""


@dataclass(frozen=True)
class StableFileSnapshot:
    """同一描述符前后身份一致时取得的完整文件快照。"""

    content: bytes
    metadata: os.stat_result


def read_stable_descriptor(
    descriptor: int,
    *,
    byte_limit: int,
) -> StableFileSnapshot:
    """在同一描述符上以读取前后 ``fstat`` 证明完整稳定快照。

    参数：``descriptor`` 是已打开文件；``byte_limit`` 是最大允许字节数。返回：
    完整字节和读取后的可信元数据。异常：非普通文件、超限、短读、读取期间身份/
    大小/时间变化或系统调用失败时抛出 ``StableFileAccessError``。
    """

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > byte_limit:
            raise StableFileAccessError("unstable_regular_file")
        first_content = _read_bounded_descriptor_once(
            descriptor,
            byte_limit=byte_limit,
        )
        after = os.fstat(descriptor)
        second_content = _read_bounded_descriptor_once(
            descriptor,
            byte_limit=byte_limit,
        )
        confirmed = os.fstat(descriptor)
    except StableFileAccessError:
        raise
    except (OSError, OverflowError, ValueError):
        raise StableFileAccessError("unstable_regular_file") from None
    if (
        len(first_content) != after.st_size
        or first_content != second_content
        or _metadata_identity(before) != _metadata_identity(after)
        or _metadata_identity(after) != _metadata_identity(confirmed)
    ):
        raise StableFileAccessError("unstable_regular_file")
    return StableFileSnapshot(content=first_content, metadata=confirmed)


def _read_bounded_descriptor_once(descriptor: int, *, byte_limit: int) -> bytes:
    """从文件起点完成一次受硬上限约束的完整读取。

    参数：``descriptor`` 是已验证普通文件；``byte_limit`` 是最大字节数。返回：
    一次完整观察的字节。异常：超限或系统读取失败由调用者统一映射。
    """

    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = bytearray()
    while len(chunks) <= byte_limit:
        chunk = os.read(
            descriptor,
            min(64 * 1024, byte_limit + 1 - len(chunks)),
        )
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > byte_limit:
        raise StableFileAccessError("unstable_regular_file")
    return bytes(chunks)


def directory_identity(path: Path) -> tuple[int, int]:
    """读取无链接或重解析点绝对目录的设备/索引节点身份。

    参数：``path`` 是待授权或复核的绝对目录。返回：设备号与索引节点元组。
    异常：路径非绝对、任一层为符号链接、目标非目录或读取失败时抛出
    ``StableFileAccessError``。
    """

    absolute = Path(os.path.abspath(path))
    try:
        if not absolute.is_absolute() or _contains_symlink(absolute):
            raise StableFileAccessError("unstable_directory")
        metadata = absolute.lstat()
    except StableFileAccessError:
        raise
    except (OSError, TypeError, ValueError):
        raise StableFileAccessError("unstable_directory") from None
    if not stat.S_ISDIR(metadata.st_mode) or is_reparse_point(metadata):
        raise StableFileAccessError("unstable_directory")
    return metadata.st_dev, metadata.st_ino


def assert_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    """复核规范目录仍是先前观察到的同一物理目录。

    参数：``path`` 是规范绝对目录；``expected_identity`` 是设备/索引节点身份。
    返回：身份一致时无返回值。异常：身份变化或目录不安全时抛出
    ``StableFileAccessError``。
    """

    if directory_identity(path) != expected_identity:
        raise StableFileAccessError("unstable_directory")


def read_regular_path(
    path: Path,
    *,
    byte_limit: int,
    missing_ok: bool,
) -> StableFileSnapshot | None:
    """不用 ``dir_fd`` 安全读取绝对路径上的稳定普通文件。

    参数：``path`` 是规范文件；``byte_limit`` 是硬上限；``missing_ok`` 决定缺失
    是否返回 ``None``。返回：稳定快照或允许的缺失。异常：父目录、目标身份、
    类型或读取稳定性无法证明时抛出 ``StableFileAccessError``。
    """

    absolute = Path(os.path.abspath(path))
    parent_identity = directory_identity(absolute.parent)
    try:
        path_metadata = absolute.lstat()
    except FileNotFoundError:
        if missing_ok:
            assert_directory_identity(absolute.parent, parent_identity)
            return None
        raise StableFileAccessError("unstable_regular_file") from None
    except OSError:
        raise StableFileAccessError("unstable_regular_file") from None
    if not stat.S_ISREG(path_metadata.st_mode) or is_reparse_point(path_metadata):
        raise StableFileAccessError("unstable_regular_file")
    descriptor = -1
    try:
        descriptor = os.open(absolute, _file_flags())
        opened_metadata = os.fstat(descriptor)
        if _physical_identity(opened_metadata) != _physical_identity(path_metadata):
            raise StableFileAccessError("unstable_regular_file")
        snapshot = read_stable_descriptor(descriptor, byte_limit=byte_limit)
        current_metadata = absolute.lstat()
        if _physical_identity(current_metadata) != _physical_identity(
            snapshot.metadata
        ):
            raise StableFileAccessError("unstable_regular_file")
        assert_directory_identity(absolute.parent, parent_identity)
        return snapshot
    except StableFileAccessError:
        raise
    except (OSError, TypeError, ValueError):
        raise StableFileAccessError("unstable_regular_file") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def regular_path_signature(path: Path, *, missing_ok: bool) -> tuple[object, ...]:
    """不用读取内容返回绝对路径普通文件的稳定身份签名。

    参数：``path`` 是规范文件；``missing_ok`` 决定缺失是否返回 ``("missing",)``。
    返回：缺失标记或设备、索引节点、大小和时间签名。异常：路径不安全、类型错误
    或观察期间变化时抛出 ``StableFileAccessError``。
    """

    absolute = Path(os.path.abspath(path))
    parent_identity = directory_identity(absolute.parent)
    try:
        before = absolute.lstat()
    except FileNotFoundError:
        if missing_ok:
            assert_directory_identity(absolute.parent, parent_identity)
            return ("missing",)
        raise StableFileAccessError("unstable_regular_file") from None
    except OSError:
        raise StableFileAccessError("unstable_regular_file") from None
    if not stat.S_ISREG(before.st_mode) or is_reparse_point(before):
        raise StableFileAccessError("unstable_regular_file")
    after = absolute.lstat()
    assert_directory_identity(absolute.parent, parent_identity)
    if _metadata_identity(before) != _metadata_identity(after):
        raise StableFileAccessError("unstable_regular_file")
    return ("file",) + _metadata_identity(after)[1:]


def ensure_child_directory(
    root: Path,
    *,
    expected_root_identity: tuple[int, int],
    child_name: str,
) -> Path:
    """不用 ``dir_fd`` 创建或复核一个固定单段直接子目录。

    参数：``root`` 与其预期身份限定父目录；``child_name`` 必须是不含分隔符的
    单段名称。返回：经复核的绝对子目录路径。异常：目录竞态、符号链接、类型
    错误或非法名称时抛出 ``StableFileAccessError``。
    """

    if (
        not child_name
        or child_name in {".", ".."}
        or Path(child_name).name != child_name
    ):
        raise StableFileAccessError("unstable_directory")
    assert_directory_identity(root, expected_root_identity)
    child = root / child_name
    try:
        child.mkdir(mode=0o755)
    except FileExistsError:
        pass
    child_identity = directory_identity(child)
    assert_directory_identity(root, expected_root_identity)
    assert_directory_identity(child, child_identity)
    return child


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """生成可检测身份与内容世代变化的元数据元组。

    参数：``metadata`` 是一次 ``stat``/``fstat`` 结果。返回：模式、设备、索引
    节点、大小、纳秒修改时间和纳秒状态变化时间。
    """

    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _physical_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """提取文件类型、设备和索引节点物理身份。

    参数：``metadata`` 是文件元数据。返回：模式类型位、设备号和索引节点。
    """

    return stat.S_IFMT(metadata.st_mode), metadata.st_dev, metadata.st_ino


def _contains_symlink(path: Path) -> bool:
    """判断绝对路径链中是否包含符号链接或 Windows 重解析点。

    参数：``path`` 是待复核绝对路径。返回：任一祖先或自身为符号链接时为
    ``True``；不存在的中间路径视为不安全并由调用者后续 ``lstat`` 拒绝。
    """

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or is_reparse_point(metadata):
            return True
    return False


def is_reparse_point(metadata: os.stat_result) -> bool:
    """判断元数据是否声明 Windows 重解析点（reparse point）。

    参数：``metadata`` 是 ``stat``/``lstat`` 结果或等价测试投影。返回：
    ``st_file_attributes`` 包含 ``0x400`` 时为 ``True``；其他平台稳定为 ``False``。
    """

    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def binary_open_flags(flags: int) -> int:
    """为原始文件描述符显式添加当前平台的二进制模式。

    参数：``flags`` 是传给 ``os.open`` 的既有标志。返回：Windows 上额外包含
    ``O_BINARY``、其他平台保持等价的标志组合，避免 CRT 把 CRLF 转换为 LF。
    """

    return flags | getattr(os, "O_BINARY", 0)


def _file_flags() -> int:
    """返回绝对路径普通文件只读所需的平台可用标志。

    参数：无。返回：只读、关闭继承、禁止跟随链接和避免 FIFO 阻塞的标志组合。
    """

    return binary_open_flags(
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


__all__ = [
    "StableFileAccessError",
    "StableFileSnapshot",
    "assert_directory_identity",
    "binary_open_flags",
    "directory_identity",
    "ensure_child_directory",
    "is_reparse_point",
    "read_regular_path",
    "read_stable_descriptor",
    "regular_path_signature",
]
