"""Windows 工作流源码底层文件访问合同测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from unilabos.workflow import (
    source_descriptor_access,
    source_file_access,
    source_publication,
)


def test_windows_regular_file_read_uses_binary_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows CRLF 文件读取不得因文本转换与 ``stat`` 大小不一致而误报。

    参数：``tmp_path`` 提供含 CRLF 的真实文件；``monkeypatch`` 在 Linux 上模拟
    Windows ``O_BINARY`` 与文本模式 ``os.read``。返回：无；稳定读取必须保留原始
    字节，并证明打开描述符时显式选择二进制模式。
    """

    source_path = tmp_path / "package.yaml"
    expected = b"package:\r\n  name: portable_lab\r\n"
    source_path.write_bytes(expected)
    binary_flag = 1 << 29
    host_binary_flag = getattr(os, "O_BINARY", 0)
    original_open = os.open
    original_read = os.read
    text_descriptors: set[int] = set()
    observed_flags: list[int] = []

    def windows_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """记录二进制标志并把测试专用位从宿主 Linux 调用中移除。"""

        observed_flags.append(flags)
        descriptor = original_open(
            path,
            (flags & ~binary_flag) | host_binary_flag,
            mode,
            dir_fd=dir_fd,
        )
        if not flags & binary_flag:
            text_descriptors.add(descriptor)
        return descriptor

    def windows_read(descriptor: int, length: int) -> bytes:
        """只对遗漏 ``O_BINARY`` 的描述符模拟 Windows CRLF 文本转换。"""

        content = original_read(descriptor, length)
        if descriptor in text_descriptors:
            return content.replace(b"\r\n", b"\n")
        return content

    monkeypatch.setattr(os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(os, "open", windows_open)
    monkeypatch.setattr(os, "read", windows_read)

    snapshot = source_file_access.read_regular_path(
        source_path,
        byte_limit=1024,
        missing_ok=False,
    )

    assert snapshot is not None
    assert snapshot.content == expected
    assert observed_flags
    assert all(flags & binary_flag for flags in observed_flags)


def test_all_workflow_source_file_open_seams_add_binary_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """描述符与发布后端必须在一个公共边界统一添加 Windows 二进制标志。"""

    binary_flag = 1 << 29
    original_open = os.open
    observed_publication_flags: list[int] = []

    def windows_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """记录发布文件打开并从宿主 Linux 调用中移除测试专用位。"""

        observed_publication_flags.append(flags)
        return original_open(
            path,
            flags & ~binary_flag,
            mode,
            dir_fd=dir_fd,
        )

    monkeypatch.setattr(os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(os, "open", windows_open)

    assert source_descriptor_access.file_flags() & binary_flag
    location = source_publication._PublicationDirectory.create(
        parent_descriptor=None,
        parent_path=tmp_path,
    )
    descriptor = location.open_child(
        "draft.py",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(descriptor)

    assert observed_publication_flags[-1] & binary_flag
