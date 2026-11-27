"""凭据文件的最小平台边界：创建时限制访问，保留各平台的持久化语义。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_WINDOWS = os.name == "nt"


def create_private_temporary(path: Path) -> tuple[int, Path]:
    """在目标同目录独占创建私密临时文件，返回调用者负责关闭的描述符。"""
    if _WINDOWS:
        from ._credential_files_windows import create_private_temporary as create

        return create(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    return descriptor, Path(name)


def create_private_directory(path: Path) -> None:
    """只创建新目录；任何已有路径（包括符号链接）均报错。"""
    if _WINDOWS:
        from ._credential_files_windows import create_private_directory as create

        create(path)
    else:
        path.mkdir(mode=0o700)


def sync_directory(path: Path) -> None:
    """POSIX 同步目录项；Windows 保留文件 fsync 和同目录原子替换。

    Windows CRT 不支持打开目录进行 fsync，也没有与 POSIX 目录 fsync 等价的
    可移植接口。这里只跳过该不支持的操作，不能吞掉文件写入或替换的真实错误，
    也不承诺 Windows 下断电后的目录项持久性。
    """
    if _WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
