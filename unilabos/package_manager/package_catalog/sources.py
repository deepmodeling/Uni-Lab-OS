"""显式工作区（Workspace）的受限本地文件来源。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal


@dataclass(frozen=True)
class WorkspaceSource:
    """由公共命令行（CLI）显式授权的一次工作区文件来源。"""

    # ``root`` 是不经过符号链接的规范工作区根目录，也是全部文件读取的授权边界。
    root: Path

    def __init__(self, root: str | Path) -> None:
        """固定不经过符号链接的工作区根目录。

        参数：``root`` 是调用者显式选择的工作区目录。
        返回：无；构造后的 ``root`` 是规范绝对路径。
        异常：目录缺失、不是目录或任一路径段是符号链接时抛出 ``ValueError``。
        """

        # ``selected_root`` 保留调用者选择的无符号链接绝对边界，禁止静默换根。
        selected_root = Path(os.path.abspath(Path(root).expanduser()))
        try:
            # ``resolved_root`` 是文件读取最终采用的规范工作区身份。
            resolved_root = selected_root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("工作区（Workspace）根目录不存在或不可访问") from error
        if (
            selected_root.is_symlink()
            or not resolved_root.is_dir()
            or resolved_root != selected_root
        ):
            raise ValueError("工作区（Workspace）根目录必须是无符号链接的目录")
        object.__setattr__(self, "root", resolved_root)

    @property
    def source_kind(self) -> Literal["workspace"]:
        """返回包来源的稳定类型。

        参数：无。
        返回：固定 wire value ``workspace``。
        异常：无。
        """

        return "workspace"

    def read_bytes(self, logical_path: str) -> bytes:
        """读取工作区根目录内的一个普通文件。

        参数：``logical_path`` 是使用 POSIX 分隔符的工作区相对文件路径。
        返回：文件的原始字节。
        异常：路径非法、缺失、越界、包含符号链接或不是普通文件时抛出
        ``ValueError``。
        """

        # ``resolved_file`` 是已验证位于授权根内且不经过符号链接的文件身份。
        resolved_file = self._resolve_regular_file(logical_path, required=True)
        assert resolved_file is not None
        try:
            return resolved_file.read_bytes()
        except OSError as error:
            raise ValueError(f"工作区文件不可读: {logical_path}") from error

    def has_file(self, logical_path: str) -> bool:
        """判断工作区内是否存在一个安全普通文件。

        参数：``logical_path`` 是使用 POSIX 分隔符的工作区相对文件路径。
        返回：文件安全存在时为 ``True``，缺失时为 ``False``。
        异常：路径非法、越界、包含符号链接或目标不是普通文件时抛出
        ``ValueError``，避免把不安全对象解释成缺失。
        """

        return self._resolve_regular_file(logical_path, required=False) is not None

    def _resolve_regular_file(
        self,
        logical_path: str,
        *,
        required: bool,
    ) -> Path | None:
        """解析并验证一个工作区相对普通文件。

        参数：``logical_path`` 是待解析相对路径；``required`` 决定缺失时是否失败。
        返回：安全文件的规范路径；仅可选且缺失时返回 ``None``。
        异常：非法路径、符号链接、目录逃逸或非普通文件抛出 ``ValueError``。
        """

        # ``logical_file`` 是完成逃逸语义校验后的 POSIX 包内路径身份。
        logical_file = _safe_logical_path(logical_path)
        # ``selected_file`` 保留授权根与逻辑路径的直接拼接结果，供符号链接检查。
        selected_file = self.root.joinpath(*logical_file.parts)
        if not selected_file.exists():
            if required:
                raise ValueError(f"工作区文件不存在: {logical_path}")
            return None
        if selected_file.is_symlink() or any(
            parent.is_symlink()
            for parent in selected_file.parents
            if parent != self.root and parent.is_relative_to(self.root)
        ):
            raise ValueError(f"工作区文件不得经过符号链接: {logical_path}")
        try:
            # ``resolved_file`` 是最终读取目标，必须仍从属于工作区授权根。
            resolved_file = selected_file.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"工作区文件不可访问: {logical_path}") from error
        if not resolved_file.is_relative_to(self.root) or not resolved_file.is_file():
            raise ValueError(f"工作区文件路径越界或不是普通文件: {logical_path}")
        return resolved_file


def _safe_logical_path(logical_path: str) -> PurePosixPath:
    """校验一个工作区逻辑路径不含逃逸语义。

    参数：``logical_path`` 是调用者提供的相对逻辑路径。
    返回：规范 ``PurePosixPath``。
    异常：绝对路径、空路径、反斜杠或父目录段抛出 ``ValueError``。
    """

    if not isinstance(logical_path, str) or not logical_path or "\\" in logical_path:
        raise ValueError("工作区逻辑路径必须是非空 POSIX 相对路径")
    # ``logical_file`` 仅表达包内逻辑身份，不解析宿主文件系统或跟随链接。
    logical_file = PurePosixPath(logical_path)
    if (
        logical_file.is_absolute()
        or not logical_file.parts
        or any(part in {"", ".", ".."} for part in logical_file.parts)
    ):
        raise ValueError(f"工作区逻辑路径非法: {logical_path}")
    return logical_file


__all__ = ["WorkspaceSource"]
